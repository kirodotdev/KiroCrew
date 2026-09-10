"""Agent configuration, themes, AIM integration, and agent CRUD handlers."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import functools
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from kiro_crew import agent_state, model_registry
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    model_registry_namespace,
    selectable_backend_values,
)
from kiro_crew.agent import (
    AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    _atomic_json_write,
    _refresh_forked_templates,
    _spec_path_is_safe,
    agents_spec_lock,
    clear_model_pin,
    emission_eligible_mcp_servers,
    get_shipped_tools,
    install_agent,
    kiro_agents_dir_path,
)
from kiro_crew.agent_discovery import (
    _read_agent_spec,
    clear_list_agents_cache,
    list_agents,
    project_agent_names,
    spec_model,
    spec_str,
)
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.drivers.acp import resolve_pin_spelling
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.apps.bridges import _mcp_lock as _agent_file_lock
from kiro_crew.apps.bridges import _registration_source
from kiro_crew.apps.manager import (
    INSTALLED_META_FILENAME,
    app_dir,
    app_enabled_state,
    apps_dir,
)
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _safe_color,
    coerce_dict_section,
    coerce_effort,
    config_path,
    inject_kiro_cli_api_key,
    normalize_agent_model,
    resolve_agent_config_path,
    resolve_agent_identity,
    resolve_effective_model,
    update_config_locked,
    write_config_atomically,
)
from kiro_crew.config.paths import data_home
from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict
from kiro_crew.config.sections import (
    _AVATAR_FILE_PIN_RE,
    _AVATAR_GHOST_BOOL_TRAITS,
    _AVATAR_GHOST_STR_TRAITS,
)
from kiro_crew.config.sections import _AVATAR_IMAGE_EXTS as _LOADER_AVATAR_IMAGE_EXTS
from kiro_crew.config.sections import (
    _safe_avatar,
)
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    _history_key_for,
    drained_to_thread,
    is_deprecated_model,
    run_config_write,
)
from kiro_crew.dashboard.handlers._shared import (
    MAX_AGENT_SKILLS,
    _capability_manager,
    _read_session_key,
    active_project_dir,
    agent_skill_keys,
    agent_skill_views,
    apply_skill_mapping,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.executors import discovery_executor, maintenance_executor, subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.member_memory_auth import require_member_memory_creation
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MemberAlreadyExists,
    UnknownMemoryStore,
    archive_member_memory_store,
    memory_store_binding_defect,
    persist_member_config,
    provision_member_memory,
    require_member_memory_not_archived,
    retire_unpublished_member_memory_store,
    rollback_member_memory_archive_if_active,
)
from kiro_crew.platform.governance import sanitize_agent_config_governance
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
    scrub_agent_subprocess_env,
    wrap_argv,
)
from kiro_crew.validation import _AGENT_NAME_RE

_MODEL_LIST_STDERR_TAIL_CHARS = 1000

logger = logging.getLogger(__name__)


def _namespaced_agent_file_exists(agent_name: str) -> bool:
    """True when an app-registered agent file backs *agent_name*.

    App agents are materialized as ``<app>--<agent>.json`` (namespaced file
    names prevent two apps' same-named agents from clobbering each other), but
    kiro-cli resolves agents by the JSON ``name`` field, not the file name. A
    file-name-only existence check therefore reports a perfectly spawnable app
    agent as missing on every boot.
    """
    # Resolved per call, not read from a module constant: the agents dir tracks
    # the live data home (see config.md "Data Home"), and a frozen constant would
    # glob the real ~/.kiro from an isolated run.
    try:
        for path in kiro_agents_dir_path().glob(f"*--{agent_name}.json"):
            data = _read_agent_spec(
                path,
                operation="api_agents_sync",
                source="dashboard",
            )
            if data is None:
                continue
            if data.get("name") == agent_name:
                return True
    except OSError:
        return False
    return False


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
    """Owner gate shared by every mutating handler in this module.

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


# ── Agent Config ──


def _find_agent_config() -> Path:
    """Find agents/defaults.json — delegates to centralized resolver."""
    return resolve_agent_config_path()


def _installed_agent_config() -> Path:
    """Return the installed agent config path (~/.kiro/agents/kirocrew.json).

    This is the live config that kiro-cli reads.  Dashboard MCP toggle
    and sync operations write here — NOT to agents/defaults.json.
    """
    return kiro_agents_dir_path() / AGENT_FILENAME


def _on_disk_mcp_servers(installed_path: Path) -> dict[str, Any] | None:
    """The installed spec's ``mcpServers`` map, or ``None`` when it cannot be read.

    ONE read, TWO rules. ``_merge_unowned_servers`` (a name ABSENT from the
    submission) and :func:`_drop_unbacked_app_entries` (a name PRESENT in a stale
    submission) are two directions of one question -- what does on-disk state say
    about this name -- so neither may read the file for itself. Two reads inside
    one commit unit could only ever agree by luck, and a rule pair disagreeing
    about its baseline is a defect that surfaces as a name both preserved and
    dropped.

    ``None`` AND ``{}`` ARE DIFFERENT ANSWERS and collapsing them is a defect --
    in EITHER direction. ``{}`` means the spec was read and holds no bridge under
    any name; ``None`` means it could not be interpreted at all. That distinction
    only matters to the present-axis rule, and there it decides the verdict:
    against ``{}`` every namespaced name the client submits is an addition the
    platform never made, while against ``None`` nothing is known and nothing may be
    decided. The absent-axis rule is indifferent -- it has nothing to preserve
    either way -- which is why its own read conflated the two harmlessly for as long
    as it was the only caller.

    A MISSING ``mcpServers`` KEY IS ``{}``, NOT ``None``, and the difference is
    load-bearing rather than cosmetic. A readable spec that simply carries no such
    key holds no bridge, which is a definite answer; reading it as "unknown" hands
    the stale-snapshot rule a reason to stand down and lets the resurrection
    through. That state is reachable from this very handler: a PUT whose submission
    omits ``mcpServers`` is persisted verbatim by ``_write_installed_config``, and
    ``_deregister_mcp_servers`` pops its entries out of ``get("mcpServers", {})``
    without ever adding the key back, so a spec can sit keyless while an old editor
    tab still holds a bridge in its snapshot.

    A ``mcpServers`` PRESENT BUT NOT AN OBJECT still answers ``None``, deliberately.
    The file parsed, but that value cannot be interpreted, and the same
    cannot-interpret state is what the submitted-side guard in
    ``_merge_unowned_servers`` refuses to act on. Deleting the client's entries on
    the strength of a value we cannot read is the guess this whole span exists to
    avoid.

    BEST-EFFORT ON AN UNREADABLE SPEC, deliberately. Missing (a first-ever write),
    corrupt, or holding a non-object ``mcpServers`` all answer ``None``: there is
    nothing authoritative to read, and this editor is the user's repair path for
    exactly that state, so failing the PUT closed would leave a broken agent
    unfixable from the dashboard. Neither rule then acts and the snapshot lands
    verbatim; enabled apps re-register their servers on the next gateway
    start (``reconcile_enabled_app_resources``), so the loss self-heals.

    The CALLER holds bridges' flock across this read and the spec write, so no
    in-gateway writer of this file can commit between them.
    """
    try:
        on_disk = json.loads(installed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(on_disk, dict):
        return None
    if "mcpServers" not in on_disk:
        return {}  # read cleanly and holds no bridge: a definite answer
    servers = on_disk["mcpServers"]
    return servers if isinstance(servers, dict) else None


def _drop_unbacked_app_entries(
    config: dict[str, Any], existing: dict[str, Any] | None
) -> tuple[str, ...]:
    """Drop submitted ``<app>:<server>`` entries the installed spec does not hold.

    THE STALE-SNAPSHOT AXIS, the mirror of ``_merge_unowned_servers``. That rule
    decides what happens to a name the submission OMITS. This one decides one
    narrow question about a name the submission CONTAINS: may the client CREATE a
    name in the app-namespace region THROUGH THIS PUT? Not while the installed spec
    is READABLE -- so a submitted namespaced name with no row on disk is dropped.
    The readability qualifier is not a hedge: an unreadable spec answers nothing, so
    the submission lands unfiltered, and a rule stated without it would contradict
    the best-effort path below.

    THE REGION IS RESERVED FROM THIS ENDPOINT, not owned by a single writer, and
    the distinction is worth stating because the weaker claim is false. Two paths
    legitimately write a ``:``-containing key into this spec: ``_register_mcp_servers``
    builds every app bridge as ``f"{app_name}:{server_name}"``, and the MCP page's
    ``handlers/mcp.py::_sync_mcp_to_agent_unlocked`` copies a global mcp.json server
    in under ``mcp_server_alias``, which returns a slash-free name UNCHANGED and so
    preserves a colon (``npm:foo`` stays ``npm:foo``). What this rule withholds is
    the ability to introduce such a name through the RAW EDITOR, where the client's
    copy is indistinguishable from a resurrection; a name either of those writers
    has actually placed on disk is present, and therefore untouched.

    THE ROW-BY-ROW TABLE LIVES IN THE SPEC, ``docs/system-specs/modules/app-kit-platform.md``
    section 1a, which is this axis's designated home; the absent axis keeps its
    table in :func:`_app_declared_server_names` instead. Only what a reader of this
    code has to know to not undo it is repeated here.

    ABSENCE FROM DISK IS A VERDICT, not a gap, and it is reached three ways --
    which is why the client's copy cannot be trusted over it:

    * the app was UNINSTALLED (``_deregister_mcp_servers`` removed the bridge and
      the app directory is gone);
    * the app is installed but DISABLED (same deregistration; startup
      reconciliation deliberately never revisits it);
    * the app is installed and ENABLED but the entry was SKIPPED on purpose --
      ``_register_mcp_servers`` refuses to write an HTTP server with no resolvable
      live port, and scrubs any stale entry for it, because a manifest's
      illustrative port is a reachable-LOOKING dead URL that kiro-cli dials on
      every request and that breaks EVERY kiro session, not just that app's.

    WHAT THIS DELIBERATELY DOES NOT DO: it never rewrites a submitted value. Where
    the name is on disk AND in the submission, the submitted row still wins
    untouched -- the editor-snapshot-wins contract, pinned by
    ``test_app_owned_entry_present_in_the_snapshot_is_updated``. Reversing it needs
    a maintainer ruling.

    THE DECLARED-NAME CENSUS IS DELIBERATELY NOT CONSULTED, and this is the one
    part a later change is most likely to undo, so the reason is here rather than
    only in the spec. The absent-axis rule must name an owner because it decides
    whether to KEEP something the client asked to remove; every candidate here is
    one the client is asking to ADD to a region it does not author, which the
    on-disk map answers by itself. A census consulted here would RESCUE the third
    case above and write back the dead URL the registration path scrubbed on
    purpose. It also keeps this rule free of manifest I/O, so it cannot raise
    :class:`AppOwnershipUnreadable` and adds no new way for a PUT to fail.

    Host-owned names are excluded, including an edition extra whose key contains
    ``:``: that key is the HOST's, and the host axis is unchanged here. Order
    against the absent-axis rule is immaterial -- a name this rule drops is by
    definition not on disk, so it cannot enter that rule's absent set.

    Returns the names it dropped, for the caller to log.
    """
    submitted = config.get("mcpServers")
    if not isinstance(submitted, dict) or not submitted:
        # Absent, empty, or a shape kiro-cli rejects outright: nothing to decide,
        # so the submission persists verbatim.
        return ()
    if existing is None:
        # The spec could not be read, so nothing is authoritative. See
        # :func:`_on_disk_mcp_servers` for why this is best-effort rather than a
        # refusal.
        return ()
    host = emission_eligible_mcp_servers()
    dropped = tuple(
        sorted(
            name for name in submitted if ":" in name and name not in existing and name not in host
        )
    )
    for name in dropped:
        del submitted[name]
    return dropped


def _merge_unowned_servers(
    config: dict[str, Any], existing: dict[str, Any] | None
) -> tuple[str, ...]:
    """Re-add ``mcpServers`` entries the submitting client does not own.

    MERGE-ON-WRITE. This PUT persists a whole-file snapshot the client read
    earlier, and ``apps/bridges.py::_register_mcp_servers`` writes app MCP
    bridges into that same file under its own flock. Without the merge, a
    registration landing between the client's read and its PUT is silently
    clobbered and the app's tools stop resolving with nothing logged anywhere.

    The rule, in one sentence: **preservation requires positive evidence of app
    or host ownership.** An on-disk entry absent from the submission is kept only
    when :func:`_app_or_host_owned` can name its owner by EXACT name; everything
    else is the client's and is deleted, which is what keeps an ordinary entry the
    user typed into this editor deletable -- including one parked under an
    installed app's namespace. If that evidence cannot be read the PUT is refused
    rather than guessed; see :func:`_app_or_host_owned`.

    The inverse test -- "keep anything no mcp.json scope declares" -- reads as
    equivalent and is not. A server added through this same editor lives ONLY in
    the installed spec, which is not a scope, so it would look unowned and be
    re-inserted on every attempt to remove it: not merely preserved against the
    user's wishes, but permanently undeletable, because each retry re-reads the
    same entry. Requiring evidence costs an app bridge nothing, since a bridge is
    always positively identifiable.

    The census is NOT consulted. Subtracting every scope-declared name from the
    candidates ahead of the ownership test can only ever remove a name that IS
    provably owned, because ownership is matched by exact manifest name: a user
    who also declares ``demo:notes`` in their own mcp.json would then make every
    stale PUT delete app ``demo``'s live bridge. Proven ownership therefore outranks a
    declaration, and a name with no proven owner is deleted whether a scope
    declares it or not -- which leaves the census unable to change any verdict.

    Two consequences worth naming rather than discovering:

    * A host-MANAGED server the rebuild RE-ADDS (``agent.emission_eligible_mcp_servers``)
      is preserved. The rebuild re-adds those entries unconditionally, so removing
      one through this editor does not stick. The qualifier is load-bearing: a
      managed entry the rebuild would NOT emit (an ``opt_in`` grant, or one whose
      ``spec_gate`` is shut) is deleted like any other absent entry, because
      nothing re-adds it and preserving it would make the grant unrevocable and
      the gated backend resurrectable.
    * An app bridge cannot be removed through this endpoint. That is the rule's
      explicit intent; the app lifecycle (disable/uninstall, which calls
      ``_deregister_mcp_servers``) is what removes it.

    BEST-EFFORT ON AN UNREADABLE SPEC, deliberately -- and the read itself
    lives in :func:`_on_disk_mcp_servers`, performed once on this rule's behalf and
    on :func:`_drop_unbacked_app_entries`'s, so both directions decide from the
    SAME bytes instead of from two reads that could disagree. A corrupt installed
    spec has no parseable entries to preserve, and this editor is the user's repair
    path for exactly that state -- failing the PUT closed would leave a broken
    agent with no way to fix it from the dashboard. So an unreadable spec preserves
    nothing and the snapshot lands verbatim; enabled apps re-register
    their servers on the next gateway start
    (``reconcile_enabled_app_resources``), so the loss self-heals.

    WHAT REMAINS. The caller holds bridges' flock across this read and the spec
    write (see :func:`_commit_agent_config`), and every writer of this file
    INSIDE the gateway takes that same flock -- ``_register_mcp_servers``,
    ``_deregister_mcp_servers``, ``reregister_app_mcp_servers``, the agent
    rebuild, and ``handlers/mcp.py``'s spec syncs -- so no app registration or
    deregistration can interleave with this read, in either direction. That
    window is closed rather than narrowed, which matters because the
    deregistration direction does not self-heal: startup reconciliation only
    re-registers ENABLED apps, so a resurrected bridge from a disabled or
    uninstalled app would persist indefinitely.

    The residual that is real is a writer OUTSIDE this process that does not take
    the flock -- kiro-cli writing the spec itself, or a user editing the file by
    hand. Nothing in the gateway can serialize against those, and the same
    exposure applies to every other writer here, so it is a property of the file
    rather than of this rule. Torn reads are not part of it: the in-process
    writers all go through ``atomic_write``, so a reader sees the whole old file
    or the whole new one.

    Returns the names it preserved, for the caller to log.
    """
    submitted = config.get("mcpServers")
    if "mcpServers" in config and not isinstance(submitted, dict):
        # A non-object ``mcpServers`` is a shape kiro-cli rejects outright.
        # Merging into it would mean inventing a map the client never sent, so
        # the submission is left exactly as-is and the existing verbatim-persist
        # behaviour (and its rejection) is unchanged.
        logger.warning(
            "Skipping agent-config merge-on-write: submitted mcpServers is %s, not an object",
            type(submitted).__name__,
        )
        return ()
    submitted_servers: dict[str, Any] = submitted if isinstance(submitted, dict) else {}
    if not existing:
        # Unreadable (``None``) or read and empty (``{}``): either way there is
        # nothing recoverable to preserve, so the two answers are equivalent HERE
        # and only here. See :func:`_on_disk_mcp_servers` for why they are not
        # equivalent to the present-axis rule, and for the BEST-EFFORT reasoning.
        return ()
    absent = {name: spec for name, spec in existing.items() if name not in submitted_servers}
    if not absent:
        return ()
    # POSITIVE EVIDENCE decides, and nothing overrides it. Ownership is the EXACT
    # manifest-declared set, so subtracting every scope-declared name from the
    # candidates BEFORE the ownership test could only ever remove a name that IS
    # provably owned -- a user who also declares ``demo:notes`` in their own
    # mcp.json would make every stale PUT delete app ``demo``'s live bridge. A
    # name with no proven owner is deleted whether a scope declares it or not, so
    # the census cannot change any verdict and is not consulted.
    owned = _app_or_host_owned(absent)
    preserved = {name: spec for name, spec in absent.items() if name in owned}
    if not preserved:
        return ()
    # Submitted first so the client's own key order is stable and the preserved
    # entries append; the two maps are disjoint by construction, so which side
    # wins is not in question.
    config["mcpServers"] = {**submitted_servers, **preserved}
    return tuple(sorted(preserved))


class AppOwnershipUnreadable(RuntimeError):
    """The app-ownership source could not be read, so nothing may be decided.

    Raised by :func:`_app_or_host_owned` and turned into a 500 with
    ``code: app_ownership_unreadable`` by the PUT. Deliberately NOT a guess in
    either direction -- see that function.
    """


def _require_present_shape(path: Path, *, expect: str, what: str) -> bool:
    """Whether *path* is genuinely ABSENT; raise when it is present but malformed.

    ``Path.is_file()`` and ``Path.is_dir()`` answer False for BOTH "nothing is
    there" and "something is there but it is the wrong kind of thing" -- a broken
    or looping symlink, a directory where a file belongs, a fifo, or a path whose
    parent denies the stat. Reading that False as absence is the
    cannot-read-becomes-not-owned defect one shape further out: a malformed
    ``installed.json`` would classify its app as not installed, and its live
    bridges would become deletable.

    Absence is proven ONLY by ``lstat`` raising ``FileNotFoundError`` -- the link
    itself, not its target, so a dangling symlink counts as present. Anything
    else that is present but not *expect* raises
    :class:`AppOwnershipUnreadable`. The follow-up ``stat`` is what makes a
    symlink to a VALID file still acceptable: ``lstat`` would call it a link and
    reject it, while ``stat`` resolves to the regular file it names.

    Returns True when the path is genuinely absent, so the caller can take its
    own not-installed branch.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True  # genuinely absent
    except OSError as exc:
        raise AppOwnershipUnreadable(f"{what} present but unstattable: {exc}") from exc
    try:
        st = os.stat(path)  # follows symlinks: a link to a valid target is fine
    except OSError as exc:
        # Dangling or looping symlink, or a permission fault on the target. The
        # entry EXISTS, so this is unreadable rather than absent.
        raise AppOwnershipUnreadable(f"{what} present but unresolvable: {exc}") from exc
    ok = stat.S_ISDIR(st.st_mode) if expect == "dir" else stat.S_ISREG(st.st_mode)
    if not ok:
        raise AppOwnershipUnreadable(f"{what} present but not a {expect}")
    return False


def _app_declared_server_names() -> frozenset[str]:
    """The exact ``<app>:<server>`` names installed, ENABLED apps DECLARE.

    Ground truth, and it has to be exact. ``_register_mcp_servers`` builds every
    key it writes as ``f"{app_name}:{server_name}"`` over
    ``manifest.mcpServers.items()``, so the manifests' declared server lists name
    precisely the entries an app can own -- nothing wider. A PREFIX test is not a
    weaker version of this: with ``demo`` installed, a client entry named
    ``demo:custom`` matches the prefix and becomes permanently undeletable.

    Read through :func:`bridges._registration_source`, which resolves a shipped
    builtin from its immutable package root rather than its mutable installed
    snapshot, so installed metadata cannot borrow a builtin's name and claim
    entries under it.

    THE COMPLETE CLASSIFICATION TABLE for an entry ABSENT from the client's
    submitted snapshot. Every reachable combination of name shape, install state,
    enablement, declaration and manifest readability appears here, so the
    classification of any absent entry is a table lookup rather than a judgement:

    ===========================  ==========================  ==============================  =====================
    Name shape                   App / metadata state        Verdict                         Pinned by
    ===========================  ==========================  ==============================  =====================
    host-managed, always-emitted  n/a (host, not an app)     PRESERVE                        test_host_managed_entry_is_preserved
    host-managed, ``opt_in``     n/a (host, not an app)      DELETE                          test_an_opt_in_managed_server_omitted_from_the_snapshot_is_deleted
    host-managed, gate CLOSED    n/a (host, not an app)      DELETE                          test_a_gate_closed_managed_server_omitted_from_the_snapshot_is_deleted
    host-managed, gate OPEN      n/a (host, not an app)      PRESERVE                        test_a_gate_open_managed_server_is_still_preserved
    host-managed, gate RAISES    n/a (host, not an app)      DELETE (gate reads closed)      test_a_managed_server_whose_gate_raises_is_deleted
    edition extra                n/a (host, not an app)      PRESERVE                        test_an_edition_contributed_server_is_preserved
    edition extra with ``:``     host-owned AND namespaced   PRESERVE (host outranks)        test_a_namespaced_edition_extra_is_preserved_by_host_ownership
    host spec not a mapping      n/a (host-produced only)    PRESERVE (no readable verdict)  test_a_malformed_host_spec_does_not_fail_the_put
    plain (no ``:``)             n/a -- no app can own it    DELETE                          test_direct_client_entry_deletes_on_a_sequential_add_then_remove
    scope-declared, app-owned    enabled, declared           PRESERVE                        test_a_scope_declaration_does_not_defeat_proven_ownership
    scope-declared, not owned    n/a -- no owner to name     DELETE                          test_a_scope_declared_name_with_no_proven_owner_is_deleted
    ``<app>:<n>``                app dir absent entirely     DELETE                          test_a_namespaced_entry_of_an_uninstalled_app_is_deleted
    ``<app>:<n>``                installed.json absent       DELETE                          test_absent_installed_metadata_is_still_skipped
    ``<app>:<n>``                installed.json corrupt      FAIL ``app_ownership_unreadable``  test_corrupt_installed_metadata_fails_the_put_and_writes_nothing
    ``<app>:<n>``                installed.json non-regular  FAIL ``app_ownership_unreadable``  test_installed_metadata_as_a_broken_symlink_fails_the_put, test_installed_metadata_as_a_directory_fails_the_put
    ``<app>:<n>``                enabled=false (disabled)    DELETE                          test_disabled_app_bridge_is_deleted
    ``<app>:<n>``                ``enabled`` field absent    treat as ENABLED, then declare  test_absent_enabled_field_counts_as_enabled
    ``<app>:<n>``                enabled, manifest unreadable  FAIL ``app_ownership_unreadable``  test_unreadable_app_manifest_fails_the_put_and_writes_nothing
    ``<app>:<n>``                enabled, NOT declared       DELETE                          test_client_entry_under_an_installed_apps_namespace_is_deleted
    ``<app>:<n>``                enabled, declared           PRESERVE                        test_a_declared_app_server_is_still_preserved
    any                          apps dir unreadable         FAIL ``app_ownership_unreadable``  test_unreadable_apps_directory_fails_the_put
    any                          apps root not a directory   FAIL ``app_ownership_unreadable``  test_apps_root_as_a_regular_file_fails_the_put
    any                          apps child unstattable      FAIL ``app_ownership_unreadable``  test_an_unstattable_apps_root_child_fails_the_put
    ===========================  ==========================  ==============================  =====================

    THE HOST ROWS ARE NOT ONE ROW, and collapsing them is a defect. A
    host-managed entry is preserved *because the rebuild re-adds it*, so the
    justification only reaches the entries the rebuild actually emits. It does not
    reach an ``opt_in`` server (``kirocrew-dashboard``: never auto-emitted, and a
    refresh keeps an existing grant current without ever re-granting a removed
    one), which preservation makes undeletable through the only surface that can
    revoke the grant; nor a server whose ``spec_gate`` is CLOSED
    (``kirocrew-computer`` on an unsupported platform, or with computer use off),
    which both spec writers ``pop`` — preserving it resurrects exactly the backend
    the gate exists to keep unspawned, and the next rebuild removes it again. The
    eligibility test is therefore the emitter's own
    (``agent.emission_eligible_mcp_servers``), not a second copy here.

    ABSENCE IS PROVEN BY ``lstat`` RAISING ``FileNotFoundError``, nothing weaker.
    ``Path.is_file()`` and ``Path.is_dir()`` answer False for a malformed path as
    readily as for a missing one, so screening on them alone reads a broken
    symlink, a directory-where-a-file-belongs, or an unstattable path as "not
    installed" and makes that app's live bridges deletable. Every present-but-wrong
    shape raises instead -- see :func:`_require_present_shape`, which screens the
    shape at the CALL SITE so ``manager.app_enabled_state`` keeps the contract its
    other callers rely on. The ENUMERATION obeys the same rule: each child of the
    apps root is stat'ed explicitly rather than filtered through ``is_dir()``,
    because pathlib routes that fault through ``_ignore_error`` and hands back a
    plain False for ENOENT, ENOTDIR, EBADF and ELOOP alike -- so a child that is a
    symlink loop reads as a regular file and is skipped, deleting the bridges
    of the app under that name. Only a resolved stat may exclude a child, and only
    by proving it is not a directory.

    Four justifications carry the rows that are not self-evident:

    * A SCOPE DECLARATION DOES NOT OUTRANK PROVEN OWNERSHIP. Subtracting every
      scope-declared name from the candidates ahead of this test could only ever
      remove a name that IS provably owned, because ownership is matched against
      EXACT manifest names: a user who also declares ``demo:notes`` in their own
      mcp.json would make every stale PUT delete app ``demo``'s live bridge. A
      declared name with no proven owner is deleted anyway, by the general rule,
      so the census cannot change a verdict and is not consulted.

    * DISABLED ⇒ DELETE. The disable lifecycle owns bridge removal
      (``_deregister_mcp_servers``), and a deregistration that FAILED during
      disable leaves a stale entry behind. Startup reconciliation only
      re-registers ENABLED apps, so it never revisits that entry: preserving it
      would keep a disabled app's code launchable through the retained bridge
      forever. Ownership therefore requires installed AND enabled.
    * ABSENT ``enabled`` FIELD ⇒ ENABLED. This matches ``apps.manager``'s own
      parse exactly -- ``InstalledApp.from_dict`` reads
      ``bool(data.get("enabled", True))`` (manager.py:170) over a dataclass whose
      default is ``enabled: bool = True`` (manager.py:114). A legacy record
      written before the field existed is treated as enabled everywhere else in
      the tree, and disagreeing here would delete the live bridges of an app the
      rest of the system considers running.
    * UNREADABLE ⇒ FAIL LOUD, never a guess. Preserving on an unreadable source
      strands undeletable entries; deleting clobbers live bridges over a fault
      that may be transient. The refusal is raised before any durable write.

    Enablement comes from :func:`manager.app_enabled_state`, whose tri-state is
    written for exactly this caller: its own docstring separates "not installed"
    and "unreadable" *because* collapsing them is "the wrong [answer] for a
    caller deciding whether to DELETE its files". ``True``/``False`` are definite
    answers and ``None`` means the metadata could not be read. ``is_app_enabled``
    and ``list_apps`` are both unusable here -- each collapses an unreadable
    record into a plain "no", which silently narrows ownership and deletes that
    app's bridges.
    """
    root = apps_dir()
    if _require_present_shape(root, expect="dir", what="installed-apps directory"):
        return frozenset()  # no apps directory at all: nothing is installed
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        raise AppOwnershipUnreadable(f"installed-apps directory unreadable: {exc}") from exc
    entries: list[Path] = []
    for child in children:
        # ONE MORE SHAPE SCREEN, for the same reason as the two above. ``p.is_dir()``
        # is unusable here: it routes its fault through pathlib's ``_ignore_error``
        # and returns a plain False for ENOENT, ENOTDIR, EBADF and ELOOP -- the
        # same False a regular file gets. A child that is a symlink LOOP would
        # then be skipped as "not an app", making the absent bridges of the app
        # under that name deletable. Only a resolved stat may exclude a child, and
        # only by PROVING it is not a directory.
        try:
            st = child.stat()  # follows symlinks, exactly as ``is_dir()`` does
        except FileNotFoundError:
            # Absence, and only absence, is a skip: an uninstall completing
            # between the listing and this stat leaves precisely this state, and a
            # DANGLING link lands here too -- unlike the metadata screen below,
            # that is a definite answer rather than an unreadable one, because no
            # app directory exists under the name at all.
            continue
        except OSError as exc:
            raise AppOwnershipUnreadable(
                f"installed-apps entry {child.name!r} present but unstattable: {exc}"
            ) from exc
        if stat.S_ISDIR(st.st_mode):
            entries.append(child)
    declared: set[str] = set()
    for entry in entries:
        # SHAPE before CONTENT. ``app_enabled_state`` reaches the metadata through
        # ``Path.is_file()``, which answers False for a broken symlink, a
        # directory, or any other non-regular file sitting at that path -- and its
        # contract turns that False into "not installed", which here would make a
        # live app's bridges deletable. Screening the shape first keeps that
        # contract intact for its other callers while giving this one the
        # present-but-malformed answer it needs.
        if _require_present_shape(
            app_dir(entry.name) / INSTALLED_META_FILENAME,
            expect="file",
            what=f"app {entry.name!r}: installed metadata",
        ):
            continue  # genuinely no installed.json: not an installed app
        enabled = app_enabled_state(entry.name)
        if enabled is None:
            raise AppOwnershipUnreadable(
                f"app {entry.name!r}: installed metadata present but unreadable"
            )
        if not enabled:
            # Not installed, or installed and deliberately disabled. Both mean no
            # ownership, so the conflation is harmless here: either way the entry
            # is the client's and stays deletable.
            continue
        manifest, _app_root = _registration_source(entry.name)
        if manifest is None:
            # bridges returns None for a manifest it could not parse. That app's
            # declared servers are UNKNOWN, not empty, and "empty" is what
            # deletes its live bridges.
            raise AppOwnershipUnreadable(f"app {entry.name!r}: manifest unreadable")
        servers = manifest.mcpServers or {}
        declared.update(f"{entry.name}:{server}" for server in servers)
    return frozenset(declared)


def _app_or_host_owned(names: dict[str, Any]) -> frozenset[str]:
    """Which of *names* an APP or the HOST provably owns.

    POSITIVE identification by EXACT NAME, and both halves of that matter. The
    inverse test -- "preserve anything no mcp.json scope declares" -- made a
    server the user typed into the raw editor permanently undeletable, because it
    lives only in the installed spec and the spec is not a scope. A prefix test
    over installed app ids reproduced the same defect for any name the client
    parked under an app's namespace. Only an exact name an owner actually claims
    is evidence.

    Two sources, both narrow:

    * HOST-managed, and only the entries a rebuild would actually RE-ADD:
      ``agent.emission_eligible_mcp_servers()`` — the always-emitted managed
      servers (cron/core) plus the edition's ``_extra_mcp_servers``. Preserving
      one is justified BY that re-add, so the set has to be the emitter's, which
      is why it is imported rather than recomputed here. The two managed entries
      a rebuild does NOT re-add are excluded and stay deletable: an ``opt_in``
      grant (``kirocrew-dashboard``) that no rebuild re-introduces, and a
      server whose ``spec_gate`` is shut (``kirocrew-computer``), which both spec
      writers actively ``pop``. Preserving those made a revocation impossible
      through the only surface that can revoke it, and resurrected a backend the
      gate exists to keep unspawned.
    * APP-declared: the exact ``<app>:<server>`` set from installed manifests --
      see :func:`_app_declared_server_names`.

    ON A FAILED READ THIS RAISES rather than guessing, because both guesses are
    wrong. Preserving every namespaced entry makes entries permanently
    undeletable; treating the declared set as empty deletes live app bridges
    over a fault that may be transient -- the very clobber merge-on-write exists
    to prevent. The PUT turns the raise
    into a 500 the client can retry, and because this runs at step (0a) before
    any durable write, all three targets stay byte-identical.

    That branch IS reachable: manifests are separate files under the apps
    directory, not covered by the installed-spec flock this unit holds, so a
    corrupt ``app.json`` or an unreadable apps directory reaches it and persists
    until repaired. It is reached whenever ANY candidate is namespaced, host-owned
    or not: the refusal is per-PUT rather than per-entry, so an edition extra
    whose name contains ``:`` is refused alongside a genuinely app-shaped one. That
    is the fail-loud direction and it is retryable, so it stays as it is.
    """
    host = emission_eligible_mcp_servers()
    owned = {name for name in names if name in host}
    namespaced = {name for name in names if ":" in name}
    if not namespaced:
        # No candidate can be app-owned, so the manifests cannot change the
        # answer and their readability is not this PUT's problem.
        return frozenset(owned)
    return frozenset(owned | (namespaced & _app_declared_server_names()))


def _write_installed_config(path: Path, config: dict[str, Any]) -> None:
    """Write the installed agent spec. The CALLER holds bridges' file lock.

    The lock lives in :func:`_commit_agent_config`, which holds it across the
    merge's on-disk READ as well as this write -- reacquiring it here would
    deadlock, because ``flock`` is per open file description and a second fd on
    the same file blocks against the first from the same thread.

    Runs in a worker thread, which is what makes the caller's synchronous
    flock legal -- on the event loop it would stall the gateway whenever app
    registration held it.
    """
    write_config_atomically(path, config)


def _commit_agent_config(
    *,
    config: dict[str, Any],
    name: str,
    mc_cfg_path: Path,
    removed_per_key: dict[str, list[str]],
    installed_path: Path,
) -> bool:
    """Perform the one fallible read and EVERY durable write of one PUT, as one unit.

    This function is the commit half of the invariant stated at the
    :func:`api_agent_config` PUT branch: it is the ONLY place that branch
    persists application state, it is purely synchronous, and it is dispatched
    exactly once through the shielded ``_offload_config_write``. Those three
    properties make a PUT **non-cancellable but not rollback-atomic**:

    * Purely synchronous — there is no await between two writes, so no
      cancellation and no other task can be interleaved into the sequence. A
      worker thread cannot be cancelled at all, so once this starts it runs to
      completion.
    * Dispatched once, shielded — the caller cannot unwind (and so cannot
      release the transaction lock) until this has returned. The alternative
      shape, awaiting each write separately under the lock, puts a cancellation
      point between writes however wide the lock is.
    * The only writer — nothing durable happens before the call, so every
      failure earlier in the handler leaves the three targets byte-identical.

    What that does NOT buy is rollback: an I/O failure (permission, quota, disk
    full, lock-open, a failed atomic rename) stops the sequence where it is, and
    the writes already committed stay committed. The honest failure prefixes, in
    order, are:

    0. the governance filter raises — nothing durable, and the caller's 500 is
       exact (it fails closed, so a raise withholds rather than grants). The
       merge-on-write step ahead of it (0a) adds one prefix of its own, and it is
       the harmless end: it can raise :class:`AppOwnershipUnreadable` while
       having mutated only the in-memory *config*, so the caller's 500 is exact
       and all three targets are byte-identical (see
       :func:`_app_or_host_owned` for why refusing beats guessing). The
       stale-snapshot rule shares that step and adds NO prefix of its own: it
       performs no I/O beyond the one read they share and cannot raise, so its
       only effect is on the in-memory *config*;
    1. the ``config.json`` read raises :class:`ConfigReadError` — nothing
       durable, and the caller's 500 is exact;
    2. the ``config.json`` write fails — nothing durable;
    3. the first bookkeeping write fails — ``config.json`` updated;
    4. the second bookkeeping write fails (the lift can write twice, once per
       key) — ``config.json`` updated plus one bookkeeping key;
    5. the installed-spec write fails — ``config.json`` and bookkeeping
       updated, the spec unchanged.

    Order inside the unit is chosen to make the *earliest* prefixes the *least*
    harmful, and four steps are load-bearing rather than incidental:

    * The merge (0a) precedes the governance filter, so the entries it re-adds
      are governed like any other — see step (0a). It is in the unit at all for
      the same reason as the read at (1): its on-disk read must be adjacent to
      the write it feeds, or an app registration landing during the flock wait
      is clobbered.
    * The governance filter is FIRST among the steps that decide what is
      persisted, and it is in here at all so that the grant decision cannot be
      made against a ceiling that changes before the write publishes it — see
      step (0). Ahead of every write because it persists nothing, so its own
      fail-closed raise costs no partial write.
    * The read is FIRST among the writes' own inputs. It is the only
      fallible-by-decision I/O step, and running it here — immediately adjacent
      to the write it feeds — is what closes the lost-update window: reading the
      baseline in the caller and writing it back one executor hop later leaves a
      gap in which a concurrent writer's unrelated fields are silently
      reverted.
    * The bookkeeping lift runs AFTER the ``config.json`` write (so prefix 2
      leaves the sidecar untouched) but BEFORE the spec write, because it STRIPS
      Kiro Crew keys (``model_managed`` / ``cc_model``) out of the same *config*
      dict the spec write then persists — reverse the two and the spec lands with
      fields kiro-cli's ``deny_unknown_fields`` rejects.

    Everything else fallible has already been decided by the caller: *config* is
    parsed and validated, *removed_per_key* is the computed ``removedTools`` map,
    and both paths are resolved.

    Returns whatever the bookkeeping lift returns (True when it stripped a
    key), so the caller can log it after the lock is released — logging is not
    durable state and has no business inside the unit.
    """
    # (0) Governance floor, immediately before the writes it governs and inside
    # the same synchronous unit, which is what its own contract asks for
    # ("every whole-config writer MUST call this immediately before it
    # persists"). Running it in phase 1 would decide against a profile snapshot
    # taken BEFORE two lock acquisitions, so a contended transaction flock —
    # unbounded, cross-process — could let the ceiling change during the wait and
    # the PUT would persist a grant governance had since withheld. Here no
    # await, no lock release and no other task can land between the decision and
    # the write that publishes it. Same reasoning that put the read at (1).
    #
    # First in the unit, so a raise from the filter (``may_skip_gate_now`` fails
    # closed) leaves all three targets byte-identical, exactly as a phase-1
    # failure does. Its SEL withhold record is infrastructure, not payload, and
    # is best-effort inside the filter — it cannot fail this unit.
    #
    # Imported lazily: platform.governance is not a module-level dependency of
    # the dashboard handlers.
    # ── THE BRIDGE-FILE LOCK SPANS THE WHOLE UNIT ─────────────────────────────
    # Acquired here rather than at the spec write, because merge-on-write reads
    # this same file and that read is only meaningful if no app writer can
    # commit between it and the write it feeds. ``_deregister_mcp_servers``
    # (app disable / uninstall / health demotion) read-modify-writes the spec
    # under exactly this flock, so an unlocked read let a PUT resurrect a bridge
    # that had just been removed -- and that direction does NOT self-heal,
    # because ``reconcile_enabled_app_resources`` only re-registers ENABLED apps
    # and skips the one whose bridge came back.
    #
    # LOCK ORDER: transaction -> config -> bridge-file. The caller already holds
    # the outer two before dispatching this unit, so widening the innermost hold
    # adds no edge and inverts nothing. The cost is that app registration waits
    # on the ``config.json`` and bookkeeping writes too -- the same accepted
    # trade ``remove_provider_entry`` documents for holding the MCP lock across
    # its unlinks, and the alternative (a second, later lock hold for just the
    # spec write) is what reopens the window above.
    #
    # Taken once. ``_write_installed_config`` deliberately does not lock: with
    # ``flock`` being per open file description, a nested reacquisition from this
    # same thread would block against this hold forever.

    with _agent_file_lock(target=installed_path):
        return _commit_agent_config_locked(
            config=config,
            name=name,
            mc_cfg_path=mc_cfg_path,
            removed_per_key=removed_per_key,
            installed_path=installed_path,
            sanitize=sanitize_agent_config_governance,
        )


def _commit_agent_config_locked(
    *,
    config: dict[str, Any],
    name: str,
    mc_cfg_path: Path,
    removed_per_key: dict[str, list[str]],
    installed_path: Path,
    sanitize: Any,
) -> bool:
    """The commit unit's steps, with bridges' file lock already held.

    Split out only so the lock acquisition reads as one statement; every
    invariant documented on :func:`_commit_agent_config` applies here, and this
    is never called from anywhere else.
    """
    # (0a) ON-DISK STATE DECIDES BOTH DIRECTIONS, immediately before the filter
    # that governs the map they produce. Inside the unit for the same reason as
    # (0) and (1): the on-disk read has to be adjacent to the write it feeds, or a
    # bridge registration landing during the (unbounded, cross-process) flock wait
    # is clobbered. BEFORE the filter, not after, so the
    # entries the merge re-adds are governed too -- re-injecting them afterwards
    # would hand an ``autoApprove`` on a preserved entry a path around step (0).
    #
    # ONE read for the two rules, so they cannot disagree about their baseline:
    # the merge decides names ABSENT from the submission, and the drop rule
    # decides namespaced names PRESENT in a stale one. Their order is immaterial (see
    # :func:`_drop_unbacked_app_entries`).
    existing = _on_disk_mcp_servers(installed_path)
    dropped = _drop_unbacked_app_entries(config, existing)
    if dropped:
        # WARNING, not info: the client submitted these and they are not being
        # persisted, which is the one outcome here a user could be surprised by.
        logger.warning(
            "agent-config PUT: dropped %d app-namespaced mcpServers entry/entries the "
            "installed spec does not hold, so a stale snapshot cannot resurrect them: %s",
            len(dropped),
            ", ".join(dropped),
        )
    preserved = _merge_unowned_servers(config, existing)
    if preserved:
        logger.info(
            "agent-config PUT: kept %d mcpServers entry/entries the client does not own: %s",
            len(preserved),
            ", ".join(preserved),
        )
    sanitize(config)

    # (1)+(2) config.json, read AND written inside one hold of the
    # ``<config>.json.lock`` sidecar. The caller's ``_get_config_lock()`` is an
    # asyncio lock: it serializes this against sibling handlers on this event
    # loop and nothing else. The ~69 ``update_config_locked`` writers -- the CLI,
    # the boot refresh, another gateway process -- take the advisory lock
    # instead, so without this the two families could interleave and whichever
    # renamed second published a document that never saw the other's change.
    #
    # Still the one fallible READ, and it still precedes every write: with the
    # default ``on_corrupt="fail"`` an unreadable config raises
    # :class:`ConfigReadError` out of here before anything durable happens, so
    # the caller's 500 stays exact. Failing closed is the point -- writing back a
    # {} baseline would drop every other setting just to record removedTools
    # (see read_config_for_update).
    def _record_removed_tools(mc_cfg: dict) -> dict:
        if removed_per_key:
            mc_cfg["removedTools"] = removed_per_key
        else:
            mc_cfg.pop("removedTools", None)
        return mc_cfg

    update_config_locked(mc_cfg_path, mutate=_record_removed_tools, stamp_meta=False)
    # (3) agent_model_state.json bookkeeping — after (2), before (4).
    changed = agent_state.lift_and_strip_bookkeeping(config, name)
    # (4) the installed spec, under the caller's bridge-file lock.
    _write_installed_config(installed_path, config)
    return changed


async def api_agent_config(request: web.Request) -> web.Response:
    """GET/PUT /api/agent/config — read or write the installed agent config.

    Reads/writes ``~/.kiro/agents/kirocrew.json`` — the live config that
    kiro-cli actually uses at runtime.  Falls back to ``agents/defaults.json``
    if the installed config doesn't exist yet.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    installed_path = _h._installed_agent_config()
    defaults_path = _h._find_agent_config()
    # Prefer installed config (what kiro-cli reads); fall back to defaults
    agent_config_path = installed_path if installed_path.is_file() else defaults_path

    if request.method == "PUT":
        denied = await _require_owner(request, "agent_config.write")
        if denied is not None:
            return denied
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        config = body.get("config")
        if not isinstance(config, dict):
            return web.json_response({"error": "config must be an object"}, status=400)
        try:
            # ── THE INVARIANT THIS BRANCH ENFORCES ────────────────────────────
            # Every validation completes BEFORE the first durable write, and the
            # one fallible read plus all durable APPLICATION/CONFIG writes of one
            # PUT execute as a single non-cancellable unit that the transaction
            # lock strictly contains — the lock cannot release while any write of
            # the unit is in flight.
            #
            # "Application/config writes" is the exact scope, and deliberately so:
            # the transaction lock's own sidecar (``_McpFileLock.__aenter__``
            # creates ~/.kiro/settings/mcp.lock) and the SEL audit record on the
            # owner-denial path above are INFRASTRUCTURE, not payload. Both can
            # become durable outside the unit, and neither is a half-applied PUT:
            # a lock file records no user setting and the audit log is required to
            # outlive the request it describes.
            #
            # The unit is non-cancellable but NOT rollback-atomic: an I/O failure
            # part-way through leaves the earlier writes committed. The prefixes
            # are enumerated in :func:`_commit_agent_config`, which also explains
            # why the order makes the earliest prefix the least harmful.
            #
            # Structurally that is two phases with nothing in between:
            #
            #   PHASE 1 (below, off the locks) — GATHER AND DECIDE. Parse, diff,
            #   resolve every path. Persists nothing, so any failure or
            #   cancellation here leaves all three target files byte-identical
            #   and the 4xx/5xx it returns is honest.
            #
            #   PHASE 2 — COMMIT. Take the transaction lock, then the config
            #   lock, then hand the GOVERNANCE FILTER, the ``config.json`` READ
            #   and ALL THREE durable writes to :func:`_commit_agent_config`
            #   through the shielded ``_offload_config_write``, exactly once.
            #   The read is inside the unit rather than in front of it: adjacent
            #   to the write it feeds, it cannot capture a baseline that a
            #   concurrent writer then updates before the worker publishes it
            #   back. The filter is inside for the same reason in the other
            #   direction: in front of the locks its verdict could go stale
            #   during a contended, unbounded flock wait, and the write would
            #   publish a grant governance had already withheld.
            #
            # Why this shape and not "the lock covers more": three prior fixes
            # widened the lock and each time the next defect was a SEQUENCING or
            # CANCELLATION fault inside the widened span — a fallible read placed
            # after a write, an await between two writes, a worker outliving the
            # await that dispatched it. Widening a span cannot fix those, because
            # they are properties of what happens INSIDE it. Collapsing the writes
            # to a single synchronous unit removes the interleaving points instead
            # of trying to cover them: there is no "between two writes" to land in.
            #
            # The three lock layers, transaction lock outermost:
            #
            # 1. ``_get_mcp_lock`` (~/.kiro/settings/mcp.lock) is the MCP
            #    TRANSACTION lock. Agent spec files are a census source for the
            #    MCP config transactions in handlers/mcp.py, which read the
            #    current state and then act on it while holding this lock. An
            #    unlocked write can land inside that read-then-act window, so the
            #    transaction commits a decision about spec contents that changed
            #    underneath it.
            # 2. ``_get_config_lock`` is the in-process lock every other
            #    ``config.json`` read-modify-writer in the dashboard takes
            #    (messaging channel savers, security, the MCP handlers, agent
            #    create/update/delete). This PUT's own RMW spans an executor
            #    hop, so the event loop does not serialize it for free: without
            #    this lock a sibling RMW can read the same baseline and the last
            #    atomic rename silently reverts the other side's unrelated
            #    settings. Held ACROSS the offload for exactly the reason
            #    api_mcp_gateway_set_stub holds it across its own offload.
            # 3. ``bridges._mcp_lock(target=installed_path)``
            #    (~/.kiro/agents/kirocrew.lock) is the FILE lock. The transaction
            #    lock does not cover it: apps/bridges.py does whole-file
            #    read-modify-writes of THIS SAME file under that separate flock
            #    (app enable/disable, MCP (de)registration). Holding only the
            #    transaction lock leaves a concurrent app enable and this PUT
            #    each writing the whole file, and the last atomic rename silently
            #    discards the other side's changes.
            #
            # Order is transaction → config → file and must stay that way. Each
            # edge already exists in the tree and none is inverted anywhere:
            # transaction→config at api_mcp_toggle / api_mcp_toggle_all /
            # api_mcp_remove in handlers/mcp.py, config→file wherever
            # ``_sync_mcp_to_agent`` runs under the config lock (api_mcp_remove,
            # api_mcp_server_detail, mcp_discover, api_capability_mcp_install),
            # and no config-lock or file-lock holder in the tree acquires the
            # transaction lock inside, so there is no ABBA cycle. The file lock is
            # taken inside the worker thread (by ``_commit_agent_config``, which
            # holds it across the whole unit so merge-on-write's on-disk READ and
            # the spec write cannot be split by an app writer) — a blocking flock
            # on the event loop would freeze the gateway while app registration
            # holds it. Widening that innermost hold changes no ORDER: the outer
            # two are already held before the unit is dispatched.
            # Nothing else in this branch takes a cross-process lock: governance +
            # SEL and agent_state take none, so running the filter inside the
            # worker adds no lock edge to the transaction → config → file order.
            #
            # PHASE 1 ── gather and decide. Nothing below is durable.
            #
            # Track tools the user intentionally removed from shipped defaults
            # so they don't reappear on upgrade.  Stored in ~/.kiro/crew/config.json
            # (NOT kirocrew.json — kiro-cli rejects unknown fields).
            # Per-key dict so removing from allowedTools only doesn't affect tools.
            #
            # Computed HERE, from the SUBMITTED config, because the governance
            # filter has not run yet: it runs in the commit unit (step 0), so
            # this diff still sees the pre-governance map. A ceiling-withheld
            # allowedTools ref is not a user removal, and diffing after the
            # filter would record it as one and suppress that tool on every
            # future upgrade. Keep this before the offload.
            shipped = get_shipped_tools()
            removed_per_key: dict[str, list[str]] = {}
            for key in ("tools", "allowedTools"):
                diff = sorted(set(shipped.get(key, [])) - set(config.get(key, [])))
                if diff:
                    removed_per_key[key] = diff
            mc_cfg_path = _h.config_path()  # type: ignore[operator]
            # Only trust a submitted name when it is a non-empty string — any
            # other JSON type (list, dict, number) would flow into the sidecar
            # helper as a dict key and crash the endpoint with a 500.
            raw_name = config.get("name")
            name = (
                raw_name if isinstance(raw_name, str) and raw_name.strip() else installed_path.stem
            )

            # Governance floor on the WHOLE-object write path: this handler
            # persists the request's config as submitted (plus the ``mcpServers``
            # entries merge-on-write re-adds, which the filter therefore also
            # governs — see ``_commit_agent_config`` step (0a)), so a dashboard
            # PUT could otherwise restore a ceiling-governed @denied grant or a
            # governed server's autoApprove that the per-ref writers strip.
            #
            # NOT in phase 1. Running the filter here would place the grant
            # decision BEFORE both lock acquisitions: the transaction flock is
            # cross-process and its wait is unbounded, so a ceiling revoked
            # during a contended wait is already stale by the time the write
            # lands, and the PUT would restore a grant governance had withheld.
            # ``_commit_agent_config`` step (0) runs it synchronously adjacent
            # to the writes it governs — see that docstring. It costs a
            # per-ref directory scan inside the locks, which is the price of the
            # decision being current; and one call, not two, so the filter is
            # never applied to a config it already filtered.

            from kiro_crew.dashboard.handlers.mcp import (
                _get_mcp_lock,
                _offload_config_write,
            )

            # PHASE 2 ── commit. Both locks are acquired ahead of every durable
            # write, so a cancellation at the (unbounded, contended) flock wait
            # inside ``__aenter__`` still tears nothing.
            async with _get_mcp_lock():
                # The config lock spans the whole read-modify-write, not just the
                # write: the read now happens in the worker thread, so this is
                # the only thing serializing this PUT's RMW against the sibling
                # ``config.json`` writers that take the same lock.
                async with _get_config_lock():
                    # THE one durable step: the config.json read + removedTools
                    # sidecar + bookkeeping sidecar + installed spec, in a worker
                    # thread, behind the shield.
                    #
                    # ``_offload_config_write`` is what binds the unit to the
                    # locks: a worker thread cannot be cancelled, and the shield's
                    # drain loop keeps re-absorbing cancellations until the worker
                    # is done, so this await cannot return or raise — and
                    # therefore ``async with`` cannot run ``__aexit__`` — while a
                    # write is still in flight. A bare ``to_thread`` per write
                    # would instead give every write boundary a cancellation point
                    # at which the locks are released with the worker still
                    # writing.
                    try:
                        changed = await _offload_config_write(
                            _commit_agent_config,
                            config=config,
                            name=name,
                            mc_cfg_path=mc_cfg_path,
                            removed_per_key=removed_per_key,
                            installed_path=installed_path,
                        )
                    except AppOwnershipUnreadable:
                        # Step (0a), ahead of every durable write, so all three
                        # targets are byte-identical and this 500 is exact.
                        # Refusing is the only honest answer: guessing preserved
                        # would make entries undeletable, guessing deleted would
                        # clobber live app bridges. The client can retry.
                        logger.exception("Refusing agent-config PUT: app ownership unreadable")
                        return web.json_response(
                            {
                                "error": "cannot determine app-owned MCP entries",
                                "code": "app_ownership_unreadable",
                            },
                            status=500,
                        )
                    except ConfigReadError:
                        # The unit's FIRST step, so this 500 is exact: no write of
                        # the unit has run and all three targets are unchanged.
                        logger.exception("Refusing to record removedTools: config unreadable")
                        return web.json_response(
                            {"error": "failed to read config file", "code": "config_unreadable"},
                            status=500,
                        )
            if changed:
                logger.info(
                    "Stripped Kiro Crew bookkeeping keys from a PUT to agent config for %r",
                    name,
                )
            # Restart kiro-cli sessions so new config takes effect
            await _h._reset_all_sessions(request)
            return web.json_response({"ok": True, "applied": True})
        except Exception as exc:
            return _err500(exc)
    # GET
    try:
        data = json.loads(agent_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    return web.json_response(data)


async def api_default_agent(request: web.Request) -> web.Response:
    """GET/PUT /api/config/default-agent — read or set the default agent."""
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if request.method == "PUT":
        denied = await _require_owner(request, "default_agent.write")
        if denied is not None:
            return denied
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        name = body.get("agent", "")
        # Reject non-strings before any use: a JSON list/object here would make
        # the membership check below raise (unhashable) into a 500, and a
        # non-string must never reach the config write either.
        if not isinstance(name, str):
            return web.json_response(
                {"error": "agent must be a string", "code": "invalid_agent_type"}, status=400
            )
        # Only a config alias may become the default: the default is resolved
        # from cfg.agents on every dispatch, so persisting any other name (a
        # project-scope discovery, an app agent, a typo) writes a default that
        # silently resolves to something else. Guarded server-side so EVERY
        # caller is covered, not just whichever picker currently hides the
        # action — project-scope rows carry scope="project" in /api/agents
        # precisely so UIs can disable this, but the config file is the last
        # line of defense.
        try:
            # Config load is stat/read/validation filesystem work; off-loop so
            # slow storage cannot freeze chat and the liveness heartbeat.
            known = set((await asyncio.to_thread(KiroCrewConfig.load)).agents.keys())
        except Exception:
            known = set()
        # Fail CLOSED: an unreadable config yields an empty `known`, and that is
        # precisely when validation is impossible — a non-empty name must be
        # rejected, not waved through. A valid config always has at least one
        # agent (load() guarantees default_agent exists in agents), so an empty
        # set never rejects a legitimate alias.
        if name and name not in known:
            return web.json_response(
                {
                    "error": f"agent {name!r} is not a configured agent alias",
                    "code": "default_agent_not_alias",
                },
                status=400,
            )
        path = _h.config_path()

        # This read-modify-write must hold the SAME in-process lock every other
        # ``config.json`` RMW in the dashboard takes (agent create/update/delete,
        # capability install/uninstall, the agent-config PUT). The event loop does
        # not serialize it for free: the PUT's own RMW runs in a WORKER
        # THREAD, holding this lock across the offload, so an unlocked read here
        # can capture a baseline the worker is about to republish — and the last
        # atomic rename silently reverts the other side's unrelated settings.
        #
        # That lock is not sufficient on its own, though: it is an asyncio lock,
        # so it serializes only same-loop callers. The read-modify-write itself
        # goes through ``update_config_locked``, which holds the
        # ``<config>.json.lock`` sidecar across its own read and write and so
        # also serializes against the CLI, worker threads and other processes.
        #
        # ``run_config_write`` is the one async entry point that holds BOTH --
        # its own docstring says so -- and it is what every other converted
        # dashboard writer uses. It takes the loop-side lock, dispatches the
        # synchronous read-modify-write to a worker thread so an unbounded
        # advisory-flock wait never stalls the gateway, and SHIELDS that worker
        # in a drain loop so the lock cannot be released with a write still in
        # flight. Composing those three by hand here would be a third copy of a
        # helper that already exists, free to drift from it.
        def _set_default(data: dict) -> dict:
            data["default_agent"] = name
            return data

        try:
            await run_config_write(
                update_config_locked, path, mutate=_set_default, stamp_meta=False
            )
        except ConfigReadError:
            # Fail closed: writing back a {} baseline would drop every other
            # setting. Nothing durable ran, so this 500 is exact.
            logger.exception("Refusing to set default agent: config unreadable")
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"},
                status=500,
            )
        return web.json_response({"ok": True, "default_agent": name})
    cfg = KiroCrewConfig.load()
    return web.json_response({"default_agent": cfg.default_agent})


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


def _is_valid_capability_package(name: str) -> bool:
    """Return True if *name* is a well-formed, non-traversal package name.

    The structural twin of ``mcp._is_valid_mcp_name``, for the package-shaped ids
    the capability seam takes. Anchoring the first character to alphanumeric is
    what makes flag injection (``--force``, ``-o``) impossible, and ``..`` is
    rejected explicitly even though the charset would admit it.
    """
    if not name or len(name) > _MAX_CAPABILITY_PACKAGE_LEN:
        return False
    if ".." in name:  # reject path traversal even if it matches the charset
        return False
    return bool(_VALID_CAPABILITY_PACKAGE_RE.match(name))


def _audit_capability(operation: str, outcome: str, resource: str) -> None:
    """Emit a SEL line naming the package a capability mutation touched.

    ``sel_audit_middleware`` already logs every mutating request, but only with
    ``resources=request.path`` — it never reads the body. That records "an agent
    package was installed" and not WHICH one. Installing an agent package
    materializes new spawnable agent configs (persisted into ``config.json`` by
    ``_do_agents_sync`` and treated as the spawn allowlist by
    ``subagent._validate_agent``) plus new skills and prompt sources, so the
    package name is the one fact an incident responder needs. Mirrors
    ``mcp_discover``'s explicit per-outcome audit.
    """
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=f"capability:{resource}",
        )
    except Exception:  # audit must never change the outcome
        logger.debug("capability audit emit failed", exc_info=True)


async def api_capability_mcp_list(request: web.Request) -> web.Response:
    """GET /api/capability/mcp — list installed MCP servers (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_mcp())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_install(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/install — install an MCP server via the capability manager."""
    denied = await _require_owner(request, "capability_mcp_install")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_mcp(server_id)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            # Off the loop: _sync_mcp_to_agent acquires bridges' synchronous
            # _mcp_lock and does a full RMW of kirocrew.json. If a concurrent app
            # registration holds that lock, a direct call would block the gateway
            # loop until it releases. Every other caller offloads — match it.
            await asyncio.to_thread(_sync_mcp_to_agent, server_id, True)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/mcp/uninstall — uninstall an MCP server via the capability manager."""
    denied = await _require_owner(request, "capability_mcp_uninstall")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    server_id = body.get("server_id", "").strip()
    if not server_id:
        return web.json_response({"error": "server_id required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_mcp(server_id)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        from kiro_crew.dashboard.handlers.mcp import (  # noqa: E402 circular: mcp imports agents
            _sync_mcp_to_agent,
        )

        async with _get_config_lock():
            # Off the loop for the same reason as install: the synchronous
            # _mcp_lock RMW must not block the gateway if app registration holds it.
            await asyncio.to_thread(lambda: _sync_mcp_to_agent(server_id, False, remove=True))
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "server_id": server_id})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_list(request: web.Request) -> web.Response:
    """GET /api/capability/skills — list installed skill packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_skills())
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_install(request: web.Request) -> web.Response:
    """POST /api/capability/skills/install — install a skill package.

    Takes only ``package``; any version/source resolution is owned by the
    edition's capability manager (no Amazon-internal version-set field is
    exposed on the public API).
    """
    denied = await _require_owner(request, "capability_skills_install")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.install_skill(package)
        if not res.ok:
            return web.json_response({"error": (res.message or "install failed")[:500]}, status=500)
        # Regenerate agent config to pick up new skill paths. install_agent()
        # does filesystem-heavy config rebuilding — offload it so it never
        # blocks the asyncio event loop (chat/heartbeat) under a slow FS.
        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_capability_skills_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/skills/uninstall — uninstall a skill package."""
    denied = await _require_owner(request, "capability_skills_uninstall")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    package = body.get("package", "").strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.uninstall_skill(package)
        if not res.ok:
            return web.json_response(
                {"error": (res.message or "uninstall failed")[:500]}, status=500
            )
        await asyncio.to_thread(install_agent)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def _mutate_agent_package(request: web.Request, *, install: bool) -> web.Response:
    """Shared body for the agent-package install/uninstall handlers.

    The two differ only in which seam op they call and the failure noun, and both
    must rebuild the agent config afterwards: an agent package carries agents plus
    its own skills and prompt sources, so the on-disk catalog and the generated
    agent config are both stale until ``install_agent()`` re-runs.

    Mirrors the guards ``mcp_discover.api_mcp_discover_install`` applies to the
    same seam: an allowlist on the name BEFORE it leaves core, ``_redact_external``
    on the manager's message, and an explicit SEL line naming the package.
    """
    denied = await _require_owner(
        request, f"capability_agent_{'install' if install else 'uninstall'}"
    )
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # A body that is valid JSON but not an object (or carries a non-string
    # ``package``) must be a 400, not an unhandled AttributeError -> 500.
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    package = body.get("package")
    if not isinstance(package, str):
        return web.json_response({"error": "package required"}, status=400)
    package = package.strip()
    if not package:
        return web.json_response({"error": "package required"}, status=400)
    # The name crosses into the edition manager verbatim, and that manager owns
    # its own invocation grammar — so bound it here rather than trusting every
    # edition to reject a traversal or a shell metacharacter. Same allowlist the
    # MCP mutation endpoints use.
    if not _is_valid_capability_package(package):
        return web.json_response({"error": f"Invalid package name '{package[:64]}'"}, status=400)
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    verb = "install" if install else "uninstall"
    try:
        res = await (mgr.install_agent(package) if install else mgr.uninstall_agent(package))
        if not res.ok:
            _audit_capability(f"capability_agent_{verb}", "error", package)
            # The message is edition subprocess output in all but name: it can
            # echo a registry URL with an embedded token, so it is redacted (both
            # scans) and length-bounded before reaching the dashboard.
            message = (res.message or f"{verb} failed")[:500]
            return web.json_response({"error": _redact_external(message)}, status=500)
        # Filesystem-heavy config rebuild — offload so it never blocks the asyncio
        # event loop (chat turn + liveness heartbeat) on a slow FS.
        await asyncio.to_thread(install_agent)
        # list_agents() caches on a (count, newest-mtime-ns) signature, so a
        # mutation landing inside one mtime tick would otherwise serve a stale
        # catalog until some unrelated write bumped the signature.
        clear_list_agents_cache()
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        _audit_capability(f"capability_agent_{verb}", "ok", package)
        return web.json_response({"ok": True, "package": package})
    except Exception as exc:
        return _err500(exc)


async def api_capability_agents_install(request: web.Request) -> web.Response:
    """POST /api/capability/agents/install — install an agent package."""
    return await _mutate_agent_package(request, install=True)


async def api_capability_agents_uninstall(request: web.Request) -> web.Response:
    """POST /api/capability/agents/uninstall — uninstall an agent package."""
    return await _mutate_agent_package(request, install=False)


async def api_capability_plugins_list(request: web.Request) -> web.Response:
    """GET /api/capability/plugins — installed plugin packages + the drift set.

    Returns the installed rows AND ``out_of_sync`` in one response: the dashboard
    renders them together (a list plus a "reconcile N packages" affordance), and
    splitting them would mean two polls that can disagree mid-install.

    The two reads are independent, so they run CONCURRENTLY rather than in
    sequence. Each carries its own ``CAPABILITY_READ_TIMEOUT`` bound, and that
    bound is sized tight precisely because the dashboard POLLS the list endpoints
    — awaiting them one after the other would let a single request pend for twice
    the designed budget and accumulate pending gateway tasks per poll, which is
    the wedge class the bound exists to prevent. Gathering caps the endpoint at
    one read bound and halves its latency.
    """
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        plugins, out_of_sync = await asyncio.gather(mgr.list_plugins(), mgr.plugins_out_of_sync())
        return web.json_response({"plugins": plugins, "out_of_sync": out_of_sync})
    except Exception as exc:
        return _err500(exc)


async def api_capability_plugins_sync(request: web.Request) -> web.Response:
    """POST /api/capability/plugins/sync — reconcile plugins with agent packages."""
    denied = await _require_owner(request, "capability_plugins_sync")
    if denied is not None:
        return denied
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        res = await mgr.sync_plugins()
        if not res.ok:
            _audit_capability("capability_plugins_sync", "error", "*")
            message = (res.message or "sync failed")[:500]
            return web.json_response({"error": _redact_external(message)}, status=500)
        state: DashboardState = request.app["state"]
        state.push_refresh("agents")
        _audit_capability("capability_plugins_sync", "ok", "*")
        # Redacted AND length-bounded on the success path too: this message names
        # what was reconciled and can carry edition subprocess output, so it gets
        # the same treatment as the failure path rather than passing through raw.
        return web.json_response(
            {"ok": True, "message": _redact_external((res.message or "")[:500])}
        )
    except Exception as exc:
        return _err500(exc)


async def api_agents_installed(request: web.Request) -> web.Response:
    """GET /api/agents/installed — list all installed kiro-cli agents.

    kirocrew is always first; kirocrew-lite is excluded.

    Deliberately GLOBAL-only (no project scope): every frontend consumer of this
    endpoint is an agent CRUD/editor surface (Agents page, template editor) whose
    actions persist into the global configuration — "Set as default" writes the
    selected name into ``cfg.agents``. A project-scope row here would let that
    action persist a name that exists only inside one checkout, producing a
    default agent the config cannot resolve. Project-scope discovery instead
    reaches the surfaces that DISPATCH agents: per-turn resolution
    (``resolve_agent_bindings(..., project_dir=...)``), spawn validation, and
    Slack — see ``agent_discovery.project_agent_names``.
    """

    # list_agents() does glob + per-file resolve(strict=True) + read_bytes +
    # json.loads over ~/.kiro/agents — blocking filesystem work that, on a large
    # agents dir (network home, many project-registry agents), can stall the
    # event loop past the loop-stall watchdog when a browser loads the dashboard.
    # Offload to the discovery pool, same as /api/skills.
    def _collect() -> list[Any]:
        agents = list(list_agents())
        agents.sort(key=lambda a: (0 if a.name == "kirocrew" else 1, a.name))
        return agents

    agents = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _collect)
    return web.json_response([a.to_dict() for a in agents])


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
    NAMESPACE gate (``model_id_namespace``) is whose ids these are. Two harnesses
    hold that capability now and their served ids do not overlap, so a retained
    claude session would otherwise answer the codex picker with claude ids --
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


def _entitled_kiro_models(request: web.Request, models: list[dict]) -> list[dict]:
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

    Fails open in every unknowable case — no live session, a backend that
    advertises nothing, or an advertised set that does not intersect the catalog
    at all (a namespace mismatch rather than an entitlement, e.g. the claude
    backend's bare ids). Filtering on any of those would empty the picker, which
    is worse than listing one model too many.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return models
    advertised: list[str] = []
    # Newest session first. `active_providers()` walks a dict of live sessions, so
    # forward order is creation order — and a session that started BEFORE a plan
    # change still holds the advertised list it captured at its own session/new.
    # Reading the oldest one would narrow the catalog to pre-downgrade
    # entitlements, i.e. keep offering exactly the models this narrowing exists to
    # hide. The most recently started session carries the most recent snapshot.
    for provider in reversed(providers):
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
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


def _codex_models(request: web.Request, configured_default: str = "") -> list[dict]:
    """Assemble the codex model dropdown from what codex-acp itself advertises.

    codex-acp has no static catalog on our side: the registry carries no codex
    namespace, and kiro-cli's ``--list-models`` names models codex refuses with a
    bare ``-32602`` at startup. The ONLY ids ``session/set_config_option("model")``
    accepts are the ones the adapter advertised as its ``model`` select on
    ``session/new``, so those are the only rows offered.

    Source order: a live CODEX session's advertised list first (the
    namespace-selected read :func:`_advertised_cc_models` does, so a retained
    claude session cannot answer with ids codex refuses), then the cross-session
    cache that :meth:`AcpClient._capture_available_models` fed on the last codex
    ``session/new`` -- so a cold dashboard after a restart still offers the real
    list instead of nothing. Both empty means no codex session has ever
    started on this install; the picker then offers ``auto`` alone, and the
    frontend refetches on the next session spawn.

    ``auto`` always leads: it means "inherit codex's own default" and is never an
    entitlement question. The configured default is resurrected only when nothing
    is known -- force-including a pin the adapter did not advertise would put back
    the exact row that kills the session.
    """
    codex_namespace = model_registry_namespace(ACP_BACKEND_CODEX)
    advertised = _advertised_cc_models(request, codex_namespace)
    if not advertised:
        cached = model_registry.advertised_models(codex_namespace)
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


async def api_models(request: web.Request) -> web.Response:
    """GET /api/models — the model list for the configured backend.

    kiro-family backends read kiro-cli's ``--list-models`` catalog (narrowed to a
    live session's entitlement); claude and codex read what their adapter
    advertised, because neither accepts an id from that catalog.
    """
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    backend = getattr(cfg.agent, "acp_backend", "")
    if backend == ACP_BACKEND_CLAUDE:
        return web.json_response(_cc_models(request, configured_default=cfg.agent.model))
    if backend == ACP_BACKEND_CODEX:
        return web.json_response(_codex_models(request, configured_default=cfg.agent.model))
    # Signed-out gateways must never reach the spawn below. kiro-cli auto-opens
    # an interactive browser login for ANY subcommand run unauthenticated
    # (--no-interactive does not suppress it, and there is no opt-out env var),
    # and the frontend polls this endpoint every 8s while the model list is
    # degraded — which is exactly the signed-out state. Ungated, that pairing
    # opened a browser window every 8s indefinitely. The 503 is the same
    # degraded response the timeout/unresolved branches already return, so the
    # client contract is unchanged; only the subprocess is skipped.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    kiro_bin: str | None = None
    try:
        from kiro_crew.acp.client import (  # noqa: F811
            _resolve_kiro_bin_for_spawn,
            _resolve_ssh_auth_sock,
        )
        from kiro_crew.env import augmented_path  # noqa: F811

        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # Degraded (binary not resolved yet), NOT a genuine "zero models"
            # result. Return 503 so the client retries instead of caching an
            # empty list — a cached [] renders an empty picker that only a
            # manual page refresh recovers from.
            return web.json_response({"error": "kiro binary not resolved"}, status=503)
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
        # path must have one posture. The explicit Kiro classification above also
        # makes the shipped "auto" tier work on Windows via Kiro's built-in
        # sandbox instead of answering 503 on every 8s poll.
        #
        # OFF the loop: `configured_sandbox_mode()` stats (and on a cache miss
        # re-reads + revalidates) config.json, and `wrap_argv` -> `detect_backend`
        # can cold-probe the backend with a synchronous
        # `subprocess.run(..., timeout=5)`. This endpoint is polled every 8s while
        # the model list is degraded, so leaving either on the loop stalls chat,
        # cron and the liveness heartbeat on exactly the host where the probe is
        # slowest. Both reads run in the worker, so the mode is resolved there
        # too rather than passed in.
        argv, cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _wrap_list_models_argv, argv
        )
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
        try:
            env = {**os.environ}
            env["PATH"] = augmented_path(env.get("PATH", ""))
            # OFF the loop: the resolver globs /tmp/ssh-*/agent.* and stats
            # every hit, so its latency scales with the /tmp entry count. Its
            # sibling wrapper's contract states it must never run on the event
            # loop, and this endpoint is polled every 8s while degraded.
            await asyncio.to_thread(_resolve_ssh_auth_sock, env)
            # The Docker entrypoint removes credentials from the long-lived
            # gateway environment.  This fixed-argv child is the official
            # kiro-cli and KIRO_API_KEY is its own model credential, so settle
            # the same single key the interactive ACP spawn receives.  Keep
            # the protected .env read off the gateway loop.
            await asyncio.to_thread(inject_kiro_cli_api_key, env)
            env = scrub_agent_subprocess_env(env)
            proc = await create_subprocess_limited(
                *argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()
                # A cold CLI spawn exceeded the timeout. This is the common
                # cause of the "picker is empty until I refresh" symptom: a
                # slow first `--list-models` spawn returning [] (HTTP 200) would
                # be cached by the client as a successful empty result. Return
                # 503 instead so React Query retries with backoff and the
                # picker self-heals without a manual refresh.
                logger.warning("api_models: --list-models timed out; returning 503")
                return web.json_response({"error": "model list timed out"}, status=503)
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
            return web.json_response({"error": "model list command failed"}, status=503)

        if not stdout.strip():
            logger.warning("api_models: --list-models returned empty output; returning 503")
            return web.json_response({"error": "model list returned empty output"}, status=503)

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "api_models: --list-models returned invalid JSON (%s); returning 503",
                exc,
            )
            return web.json_response({"error": "model list returned invalid JSON"}, status=503)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            logger.warning("api_models: --list-models returned an invalid payload; returning 503")
            return web.json_response(
                {"error": "model list returned an invalid payload"}, status=503
            )
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
        if model_registry.refresh_kiro_windows(models):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), model_registry.persist_kiro_windows
            )
        models = [m for m in models if not is_deprecated_model(m.get("model_name", ""))]
        models = _entitled_kiro_models(request, models)
        return web.json_response(models)
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
            if f"/{c}" not in _BLOCKED_SLASH_COMMANDS
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
            for c in sorted(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)
        ]
    )


# A published template's filename is its permanent identity (no rename), so the
# name is validated up front. Same charset the fork sanitizer produces, plus a
# length cap that keeps the filename portable.
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

# Windows reserves these basenames (before the first dot, any extension) at the
# filesystem level: creating CON.json raises, and some transports mangle them.
# Checked wherever a template filename is chosen — user-supplied publish names
# are refused, generated fork names are suffixed past them.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_reserved_basename(name: str) -> bool:
    return name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES


def _write_spec_file(dest: Path, data: dict) -> None:
    """Exclusive create: 'x' refuses an existing destination — including a
    differing-case sibling on case-insensitive filesystems — instead of
    truncating it. Serialization happens here too so callers offload BOTH
    to a thread; a near-limit spec would otherwise stall the event loop.

    Governance runs HERE, at the single writer both fork and publish use:
    a copied spec carries its source's ``allowedTools``/``autoApprove``
    verbatim, and those are the two routes that skip the PreToolUse gate —
    per ``sanitize_agent_config_governance``'s contract, every whole-config
    writer must filter immediately before persisting.
    """
    sanitize_agent_config_governance(data)
    try:
        with open(dest, "x", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
    except FileExistsError:
        # The exclusive create lost the race: dest is a CONCURRENT CREATOR'S
        # file, never ours to remove.
        raise
    except BaseException:
        # A partial write (ENOSPC) would leave a truncated, unparseable spec
        # that every later create refuses as name_taken. The create is ours
        # (exclusive), so removing it on failure is always safe. Unlink AFTER
        # the with-block closed the handle — Windows refuses to unlink an
        # open file with a sharing violation.
        with contextlib.suppress(OSError):
            dest.unlink()
        raise


class _AmbiguousTemplateName(Exception):
    """More than one spec file resolves to the requested name."""


def _load_template_specs(
    agents_dir: Path, name: str, operation: str
) -> tuple[dict[str, Any] | None, str, set[str], Path | None]:
    """Find the source spec and every name a new spec must not collide with.

    ``taken`` is case-folded: 'Reviewer' and 'reviewer' are the same file on
    the case-insensitive filesystems macOS and Windows default to. The source
    PATH is returned too so create closures can RE-READ it inside
    ``agents_spec_lock`` — this pre-lock snapshot can go stale against a
    concurrent fork refresh. A name matching MORE THAN ONE file (one by stem,
    another by declared name) raises ``_AmbiguousTemplateName``: glob order
    would otherwise pick silently, and the copy could carry the wrong
    template's contents.
    """
    source: dict[str, Any] | None = None
    source_name = name
    source_path: Path | None = None
    taken: set[str] = set()
    matches: list[Path] = []
    for f in sorted(agents_dir.glob("*.json")):
        # An unreadable spec still occupies its filename.
        taken.add(f.stem.lower())
        spec = _read_agent_spec(f, operation=operation, source="dashboard")
        if spec is None:
            continue
        declared = spec_str(spec, "name")
        if declared:
            taken.add(declared.lower())
        if declared == name or f.stem == name:
            matches.append(f)
            if source is None:
                source = spec
                source_name = declared or f.stem
                source_path = f
    if len(matches) > 1:
        raise _AmbiguousTemplateName(name)
    return source, source_name, taken, source_path


class _StaleBinding(Exception):
    """The crew moved off the expected template between validation and write."""


class _ForeignPrivateCopy(Exception):
    """The target became another crew's private copy before the write landed."""

    def __init__(self, owner: str):
        super().__init__(owner)
        self.owner = owner


class _PublishNameBound(Exception):
    """A crew binding references the requested publish name with no file
    behind it — creating the file would make that binding resolve to it."""


class _ForkBookkeepingFailed(Exception):
    """Sidecar lineage recording failed inside the fork's lock hold; the
    created file was already unwound."""


def _rebind_crew_locked(
    crew: str,
    expected: tuple[str, ...] | None,
    new_target: str,
    require_path: Path | None = None,
) -> None:
    """Apply ONLY the binding delta to config.json, under the advisory lock.

    A full ``cfg.save()`` snapshot races every other config writer (CLI,
    settings PUTs): it re-writes fields from a load taken before this
    handler's awaits, silently reverting concurrent changes. The stale-binding
    check re-runs inside the critical section, so the 409 also covers a rebind
    that landed after the handler's own validation read. ``expected=None``
    skips the staleness check (a deliberate last-write-wins switch); the write
    stays binding-only and locked either way.

    ``require_path``: the target's spec file, rechecked for existence INSIDE
    the critical section under the spec lock. A cross-process delete between a
    handler's validation and this write would otherwise persist a binding to
    nothing; a vanished file raises ``FileNotFoundError``.
    """

    def _mutate(data: dict) -> dict | None:
        entry = data.get("agents", {}).get(crew)
        if not isinstance(entry, dict) or (
            expected is not None and entry.get("kiro_agent") not in expected
        ):
            raise _StaleBinding()
        if require_path is not None:
            with agents_spec_lock(require_path.parent):
                if not require_path.exists():
                    raise FileNotFoundError(new_target)
        if entry.get("kiro_agent") == new_target:
            return None
        # Checked INSIDE the critical section, like the staleness check: a
        # fork recording lineage after a handler's pre-validation must not
        # slip another crew's private copy into this binding.
        if owner := _foreign_private_copy_owner(crew, new_target):
            raise _ForeignPrivateCopy(owner)
        entry["kiro_agent"] = new_target
        return data

    update_config_locked(mutate=_mutate)


class _UnverifiableLineage(Exception):
    """The sidecar or spec dir could not be read while checking whether a
    binding target is a private copy — ownership cannot be verified, so the
    binding is refused rather than allowed."""


def _foreign_private_copy_owner(crew: str, target: str) -> str | None:
    """The owning crew's name when *target* is ANOTHER crew's private copy.

    Lineage means one crew's edits land on that file: binding a second crew to
    it has publish/reset cleanup delete the second crew's live template out
    from under it. STRICT read: a lenient read let a corrupt
    sidecar degrade to an allowed bind, and the spawn gate — which validates
    governance, not ownership — would then run the foreign crew's sessions on
    the private definition once the sidecar recovered. An unverifiable read
    raises ``_UnverifiableLineage``; every binding writer maps it to a 409.
    """
    try:
        info = agent_state.get_fork_info(target, strict=True)
        if info is None and target:
            # Lineage is keyed by the DECLARED name, but a binding can carry
            # the file STEM where the two differ — and that binding resolves
            # the same file. Resolve the target to its declared name before
            # concluding "not a copy" (the bind-side twin of the
            # cleanup's stem coverage). Ambiguity or an unreadable dir raises
            # like an unreadable sidecar — fail closed.
            _spec, declared, _taken, spec_path = _load_template_specs(
                kiro_agents_dir_path(), target, "foreign_private_copy_check"
            )
            if spec_path is not None and declared != target:
                info = agent_state.get_fork_info(declared, strict=True)
    except Exception as exc:
        raise _UnverifiableLineage(target) from exc
    owner = (info or {}).get("private_to")
    return owner if isinstance(owner, str) and owner and owner != crew else None


def _reserved_binding_names(cfg_data: dict) -> set[str]:
    """Every name the config currently resolves a session against, case-folded:
    each crew's ``kiro_agent`` plus the legacy global fallback
    ``agent.default_agent``. Creating a spec file under any of
    these makes a dangling reference resolve to it, so destination-name checks
    treat them all as reserved."""
    names = {
        str(entry.get("kiro_agent")).lower()
        for entry in cfg_data.get("agents", {}).values()
        if isinstance(entry, dict) and entry.get("kiro_agent")
    }
    agent_section = cfg_data.get("agent")
    if isinstance(agent_section, dict):
        fallback = agent_section.get("default_agent")
        if isinstance(fallback, str) and fallback:
            names.add(fallback.lower())
    return names


def _unlink_copy_unless_referenced(copy_file: Path, agents_dir: Path, *names: str) -> str:
    """Delete a superseded private copy UNLESS a crew binding still resolves it
    — reference check and unlink as ONE critical section under the config
    advisory lock, so a concurrent binding write cannot land between them.

    Cleanup callers rebound their own crew away before asking, so any hit is a
    FOREIGN binding (pre-dating the bind-time guard) and deleting the file
    would break that crew's sessions with "Mode not found". Returns the
    outcome — ``"deleted"``, ``"referenced"``, or ``"error"`` (unlink failure
    or unreadable config, both failing closed with the file kept) — because
    callers treat the retention reasons differently: a REFERENCED file is in
    live use and must stay as it is, while an error-retained one may need
    lineage marking so private content does not surface as a shared template.
    """
    outcome = "error"
    targets = set(names)

    def _check_then_unlink(data: dict) -> None:
        nonlocal outcome
        for entry in data.get("agents", {}).values():
            if isinstance(entry, dict) and entry.get("kiro_agent") in targets:
                logger.warning(
                    "another crew is still bound to private copy %r; leaving it in place",
                    copy_file.stem,
                )
                outcome = "referenced"
                return None
        try:
            with agents_spec_lock(agents_dir):
                copy_file.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove superseded copy %r", copy_file.stem, exc_info=True)
            return None
        outcome = "deleted"
        # None: the reference check mutates nothing — the lock is held for
        # isolation against binding writers, not for a config write.
        return None

    try:
        update_config_locked(mutate=_check_then_unlink)
    except Exception:
        logger.warning(
            "config unreadable during private-copy cleanup; keeping %r",
            copy_file.stem,
            exc_info=True,
        )
        return "error"
    return outcome


async def api_agent_fork(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/fork — give one crew a private copy of a template.

    Blueprint semantics: a crew's definition edits must not mutate the shared
    template file that other crews (and kiro-cli) read. The first edit forks a
    copy named after the crew, records lineage in the agent_state sidecar (the
    spec itself cannot carry it — kiro-cli rejects unknown fields and drops the
    whole agent), and rebinds the crew. All of it happens under the config lock
    so the agents sync loop can never observe the new file unbound and
    auto-create a ghost agent for it.
    """
    name = request.match_info["name"]
    denied = await _require_owner(request, "agent_detail.fork")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    crew = body.get("crew")
    if not isinstance(crew, str) or not crew.strip():
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)
    crew = crew.strip()

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()

        try:
            source, source_name, taken, source_path = await asyncio.to_thread(
                _load_template_specs, agents_dir, name, "api_agent_fork"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if source is None:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        agent = cfg.agents[crew]
        # A stale or racing request must not clobber a newer binding: the fork
        # was issued against the crew's current template, so require it still is.
        if agent.kiro_agent not in (name, source_name):
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )

        # Already this crew's own copy: nothing to fork. Idempotence keeps the
        # frontend's fork-before-first-edit call safe to repeat.
        fork = agent_state.get_fork_info(source_name)
        if fork and fork["private_to"] == crew:
            return web.json_response({"ok": True, "template": source_name, "already_private": True})

        # The copy is named after the crew — the fork is invisible, so there is
        # no naming step, and the crew's name is the one the user already knows.
        # Sanitized because crew names are free text and this becomes a filename
        # (a template's permanent identity; there is no rename). The declared
        # "name" is set equal to the stem below, which is what keeps discovery's
        # package-filename guess from misreading a dashed copy name.
        # Bounded to keep the filename (plus a collision suffix) inside the
        # 63-char template-name rule and every filesystem's component limit.
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", crew)[:48].strip("-.") or "agent"
        # The specs Kiro Crew itself generates (kirocrew.json, kirocrew-lite.json,
        # ...) are rebuilt on boot; a copy landing on one of those stems while
        # the managed file is absent would be overwritten by that rebuild, so
        # they count as taken whether or not the file exists right now. Same
        # rule the publish handler applies to a user-chosen name.
        managed_stems = {Path(f).stem.lower() for f in OWNED_KIRO_AGENT_FILES}

        def _create_record_bind() -> tuple[str, Path]:
            """Create the file, record lineage, and rebind in ONE config-lock
            hold: released between those steps, a locked bind
            could land on the just-created file before its ownership existed,
            and the lineage would then record one owner while another crew is
            bound — every later edit silently hitting both. Spec lock inner,
            the same nesting every cleanup path uses; the sidecar writers
            never take the config lock, so the nesting cannot invert.

            `taken` is a pre-scan and can go stale; the in-lock exists() probe
            asks the filesystem with its own case semantics, and the exclusive
            create refuses whatever both still missed rather than truncating
            it. The SOURCE is re-read in-lock too: the pre-lock snapshot can
            miss a concurrent refresh's writes. Reserved Windows basenames
            (CON, NUL, …) are suffixed past like collisions. Every current
            binding is reserved as well — a crew bound to a MISSING name would
            otherwise capture the new copy, and the legacy
            global fallback `agent.default_agent` is a resolvable reference
            like any binding.
            """
            chosen: list[tuple[str, Path]] = []

            def _unwind(dest: Path, copy_name: str) -> None:
                # Locked writers cannot have bound the copy — we hold the
                # config lock — but a writer outside it (a hand-edited file,
                # a process that skips the sidecar lock) can, so the reference
                # check re-reads the FILE before unlinking.
                try:
                    raw = json.loads(config_path().read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
                targets = {copy_name, dest.stem}
                for entry in raw.get("agents", {}).values():
                    if isinstance(entry, dict) and entry.get("kiro_agent") in targets:
                        logger.warning(
                            "a crew bound private copy %r mid-fork; leaving it in place",
                            copy_name,
                        )
                        with contextlib.suppress(Exception):
                            agent_state.prune(copy_name)
                        return
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
                with contextlib.suppress(Exception):
                    agent_state.prune(copy_name)

            def _mutate(cfg_data: dict) -> dict:
                # Staleness FIRST: nothing is created for a bind that moved.
                entry = cfg_data.get("agents", {}).get(crew)
                if not isinstance(entry, dict) or entry.get("kiro_agent") not in (
                    name,
                    source_name,
                ):
                    raise _StaleBinding()
                bound = _reserved_binding_names(cfg_data)
                with agents_spec_lock(agents_dir):
                    if source_path is None:
                        raise FileNotFoundError(source_name)
                    fresh_source = _read_agent_spec(
                        source_path, operation="api_agent_fork", source="dashboard"
                    )
                    if fresh_source is None:
                        raise FileNotFoundError(source_path)
                    copy_name, suffix = base, 2
                    while (
                        copy_name.lower() in taken
                        or copy_name.lower() in bound
                        or copy_name.lower() in managed_stems
                        or _is_reserved_basename(copy_name)
                        or (agents_dir / f"{copy_name}.json").exists()
                    ):
                        copy_name = f"{base}-{suffix}"
                        suffix += 1
                    data = dict(fresh_source)
                    data["name"] = copy_name
                    # Same rule as every other spec writer: bookkeeping keys
                    # never reach a kiro spec.
                    agent_state.lift_and_strip_bookkeeping(data, copy_name)
                    dest = agents_dir / f"{copy_name}.json"
                    _write_spec_file(dest, data)
                    # Lineage inside the SAME hold. The prune is NOT
                    # suppressed: a stale sidecar entry from a failed earlier
                    # delete must not ship on the new copy.
                    try:
                        agent_state.prune(copy_name)
                        agent_state.set_fork_info(
                            copy_name, forked_from=source_name, private_to=crew
                        )
                        managed = agent_state.get_model_managed(source_name)
                        if managed is not None:
                            agent_state.set_model_managed(copy_name, managed)
                    except Exception:
                        logger.exception("fork bookkeeping failed for %r", copy_name)
                        _unwind(dest, copy_name)
                        raise _ForkBookkeepingFailed() from None
                    entry["kiro_agent"] = copy_name
                    chosen.append((copy_name, dest))
                return cfg_data

            update_config_locked(mutate=_mutate)
            return chosen[0]

        try:
            copy_name, dest = await asyncio.to_thread(_create_record_bind)
        except FileNotFoundError:
            return web.json_response(
                {
                    "error": f"Template '{source_name}' changed on disk; retry.",
                    "code": "source_changed",
                },
                status=409,
            )
        except _StaleBinding:
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _ForkBookkeepingFailed:
            return web.json_response(
                {"error": "Could not record the copy's lineage", "code": "bookkeeping_failed"},
                status=500,
            )
        except Exception:
            logger.exception("fork failed for crew %r", crew)
            return web.json_response(
                {"error": "Could not create the copy", "code": "fork_failed"},
                status=500,
            )

    # A refresh pass can interleave between the lineage record and the rebind:
    # it then sees a fork with no corroborating binding, records it as failed,
    # and the spawn gate blocks the copy the user just forked. Re-running the
    # refresh AFTER the binding persisted re-corroborates it and rebuilds the
    # failure set. Outside the spec lock — the pass takes it
    # per fork. Best-effort for the RESPONSE only: the fork is committed
    # either way, and a refresh failure leaves the gate fail-closed (the
    # correct posture) rather than turning a committed fork into a 500.
    try:
        await asyncio.to_thread(_refresh_forked_templates)
    except Exception:
        logger.warning("post-fork governance refresh failed", exc_info=True)
    clear_list_agents_cache()
    state.push_refresh("agents")
    return web.json_response(
        {"ok": True, "template": copy_name, "filename": dest.name, "forked_from": source_name}
    )


async def api_agent_publish(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/publish — save a crew's private copy as a named template.

    The counterpart of the invisible fork: forking never asks for a name, so
    the one place a template name is ever chosen is here, deliberately, by the
    user. Publishes {name} (which must be *crew*'s private copy) under the
    caller-supplied new name with NO fork lineage — a real, shareable template —
    rebinds the crew to it, and removes the superseded private copy. A filename
    is a template's permanent identity (there is no rename), which is why the
    name is validated and collision-refused rather than suffixed.
    """
    name = request.match_info["name"]
    denied = await _require_owner(request, "agent_detail.publish")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    crew = body.get("crew")
    new_name = body.get("name")
    if not isinstance(crew, str) or not crew.strip():
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)
    if not isinstance(new_name, str) or not _TEMPLATE_NAME_RE.match(new_name.strip()):
        return web.json_response(
            {
                "error": "name must be 1-63 letters, digits, dots, dashes or underscores",
                "code": "invalid_template_name",
            },
            status=400,
        )
    if _is_reserved_basename(new_name.strip()):
        # A filename Windows reserves at the filesystem level (CON, NUL, COM1…):
        # creating CON.json raises there, so the name can never be portable.
        return web.json_response(
            {
                "error": f"'{new_name.strip()}' is a reserved filename on Windows",
                "code": "invalid_template_name",
            },
            status=400,
        )
    crew = crew.strip()
    new_name = new_name.strip()
    if f"{new_name.lower()}.json" in {f.lower() for f in OWNED_KIRO_AGENT_FILES}:
        return web.json_response(
            {"error": f"'{new_name}' is reserved", "code": "template_name_reserved"}, status=400
        )

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()

        try:
            source, source_name, taken, source_path = await asyncio.to_thread(
                _load_template_specs, agents_dir, name, "api_agent_publish"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if source is None:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )
        # Case-folded: 'Reviewer' and 'reviewer' are the same file on the
        # case-insensitive filesystems macOS and Windows default to.
        if new_name.lower() in taken:
            return web.json_response(
                {"error": f"A template named '{new_name}' already exists", "code": "name_taken"},
                status=409,
            )

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        # Only a private copy can be published: publishing a template that is
        # already shared would silently duplicate it, and publishing another
        # crew's copy would leak their customization.
        fork = agent_state.get_fork_info(source_name)
        if not fork or fork["private_to"] != crew:
            return web.json_response(
                {
                    "error": f"'{source_name}' is not {crew}'s private copy",
                    "code": "not_a_private_copy",
                },
                status=409,
            )
        # A stale publish must not rebind over a newer binding (same guard as
        # fork). Fast pre-check only; the authoritative check re-runs inside
        # _rebind_crew_locked's critical section.
        if cfg.agents[crew].kiro_agent not in (name, source_name):
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )

        def _create_published() -> Path:
            """Create under the spec lock; the exclusive write refuses any
            destination the pre-scan missed instead of truncating it. The
            source is re-read in-lock: the pre-lock snapshot can miss a
            concurrent refresh's writes and publish stale content.

            Runs under the config lock (outer) so a name some crew's binding
            references — with no file behind it — is refused before the file
            exists: the moment it does, that dangling binding resolves to it
            and the crew silently executes the published content (GPT
            round-52). Publish never suffixes, so this rejects. The
            publishing crew's own binding cannot hit: it points at the source,
            whose file exists and is caught by the name_taken pre-check.
            """
            created: list[Path] = []

            def _check_bindings_then_create(cfg_data: dict) -> None:
                # Includes the legacy global fallback agent.default_agent —
                # a resolvable reference like any crew binding.
                if new_name.lower() in _reserved_binding_names(cfg_data):
                    raise _PublishNameBound()
                with agents_spec_lock(agents_dir):
                    if source_path is None:
                        raise FileNotFoundError(source_name)
                    fresh_source = _read_agent_spec(
                        source_path, operation="api_agent_publish", source="dashboard"
                    )
                    if fresh_source is None:
                        raise FileNotFoundError(source_path)
                    data = dict(fresh_source)
                    data["name"] = new_name
                    dest = agents_dir / f"{new_name}.json"
                    if dest.exists():
                        raise FileExistsError(dest)
                    # Lineage BEFORE the file exists: from its first byte on
                    # disk the destination is this crew's private copy, so no
                    # failure path between here and the rebind — a rollback
                    # whose unlink AND private-marking both fail included —
                    # can leave it discoverable as a shared template. The
                    # clean-slate prune runs first so a dead entry under this
                    # name (stale lineage, stale model state) is not inherited;
                    # its failure aborts the publish before anything is
                    # written. Success flips the copy to shared
                    # (``clear_fork_info``) once the crew is bound to it.
                    agent_state.prune(new_name)
                    agent_state.set_fork_info(new_name, forked_from=source_name, private_to=crew)
                    agent_state.lift_and_strip_bookkeeping(data, new_name)
                    try:
                        _write_spec_file(dest, data)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            agent_state.prune(new_name)
                        raise
                    created.append(dest)
                # None: the binding read mutates nothing — the lock is held so
                # no binding write can land between the check and the create.
                return None

            update_config_locked(mutate=_check_bindings_then_create)
            return created[0]

        try:
            dest = await asyncio.to_thread(_create_published)
        except _PublishNameBound:
            return web.json_response(
                {
                    "error": f"A crew is bound to the name '{new_name}'",
                    "code": "name_bound",
                },
                status=409,
            )
        except FileExistsError:
            # A concurrent creator won the name between our pre-scan and the
            # exclusive create. Publish never suffixes: the name is the user's.
            return web.json_response(
                {"error": f"A template named '{new_name}' already exists", "code": "name_taken"},
                status=409,
            )
        except FileNotFoundError:
            return web.json_response(
                {
                    "error": f"Template '{source_name}' changed on disk; retry.",
                    "code": "source_changed",
                },
                status=409,
            )
        except Exception:
            # The sidecar prune / lineage write failed before the destination
            # existed: nothing to undo, the private copy and binding are intact.
            logger.exception("publish bookkeeping failed for %r", new_name)
            return web.json_response(
                {"error": "Could not record the template's lineage", "code": "bookkeeping_failed"},
                status=500,
            )

        # Undo for every post-create failure (bookkeeping OR rebind): locked.
        # Unlink FIRST; when the destination cannot be removed (locked file),
        # it stays what the create step already made it — this crew's private
        # copy — instead of surfacing as a shared template.
        def _undo_publish() -> None:
            # Reference-aware, same helper as the superseded-copy cleanup:
            # another crew can bind the just-created destination before this
            # rollback runs, and unlinking it then breaks that crew's sessions
            # with "Mode not found". The check and the unlink are one critical
            # section under the config lock, so a binding writer cannot land
            # between them. A RETAINED destination already carries the
            # private lineage the create step wrote, so even a failing
            # re-assert here leaves nothing discoverable as shared.
            outcome = _unlink_copy_unless_referenced(dest, agents_dir, new_name, dest.stem)
            if outcome == "deleted":
                with contextlib.suppress(Exception):
                    agent_state.prune(new_name)
                return
            if outcome == "referenced":
                # Another crew adopted the destination before the rollback:
                # it is in live use as a shared template, so it stays one —
                # deleting it or leaving it marked private would break or
                # misattribute that crew's binding.
                with contextlib.suppress(Exception):
                    agent_state.clear_fork_info(new_name)
                return
            logger.warning("rollback could not remove %r; it stays private to %r", new_name, crew)
            with contextlib.suppress(Exception):
                agent_state.set_fork_info(new_name, forked_from=source_name, private_to=crew)

        # Offloaded: the sidecar mutators take a blocking cross-process
        # file_lock(wait=True), which must never run on the event loop.
        def _record_publish_model() -> None:
            # The clean-slate prune already ran in the create step, before
            # the file existed; only the source's model tracking is copied.
            managed = agent_state.get_model_managed(source_name)
            if managed is not None:
                agent_state.set_model_managed(new_name, managed)

        try:
            await asyncio.to_thread(_record_publish_model)
        except Exception:
            logger.exception("publish bookkeeping failed for %r", new_name)
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {"error": "Could not record the template's model", "code": "bookkeeping_failed"},
                status=500,
            )

        try:
            await asyncio.to_thread(_rebind_crew_locked, crew, (name, source_name), new_name)
        except _StaleBinding:
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _UnverifiableLineage:
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{new_name}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        except Exception:
            logger.exception("publish rebind failed for crew %r", crew)
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {"error": "Could not update the crew's binding", "code": "rebind_failed"},
                status=500,
            )

        # The binding has moved: the destination is committed, so drop the
        # private lineage the create step wrote and let it list as the shared
        # template the user published. Nothing is exposed if this fails — the
        # crew is bound to what is then still its own private copy under the
        # new name — and the publish is COMMITTED (rebind persisted, file
        # created), so this must not become an HTTP error: the client keys its
        # editor off the response's template name, and an error would leave it
        # targeting the superseded copy while the crew runs the new one, so its
        # later edits would land on an inactive file. Reported as a warning on
        # the committed result instead; the stale lineage row is reconciled by
        # the next refresh sweep, like the prune below.
        publish_warning: str | None = None
        try:
            await asyncio.to_thread(agent_state.clear_fork_info, new_name)
        except Exception:
            logger.exception("publish could not mark %r shared", new_name)
            publish_warning = "publish_incomplete"

        def _cleanup_superseded() -> None:
            """Remove the superseded private copy — file first, lineage after.

            Under the spec lock like every other spec mutation; lineage
            outlives a failed delete, since pruning it while the file remains
            would surface the private customization as a shared template.
            Unlinks the RESOLVED ``source_path`` — a fork whose file stem
            differs from its declared name would be missed by
            ``{source_name}.json``, leaving the file while its lineage is
            pruned (the exact surface-as-shared failure above).
            """
            if source_path is None or not _spec_path_is_safe(source_path, agents_dir):
                return
            # This crew was rebound to the published name above, so any binding
            # still naming the copy is another crew's (pre-dating the bind-time
            # guard): the locked helper keeps file AND lineage in that case,
            # atomically against concurrent binding writes.
            # Stem included: a stem binding resolves the same file.
            if (
                _unlink_copy_unless_referenced(
                    source_path, agents_dir, source_name, source_path.stem
                )
                != "deleted"
            ):
                return
            # Non-throwing: the publish is already committed (rebind persisted,
            # file created). A sidecar failure here must not turn a committed
            # publish into an HTTP 500 whose retry then 404s; the stale lineage
            # row is reconciled by the next refresh sweep.
            with contextlib.suppress(Exception):
                agent_state.prune(source_name)

        await asyncio.to_thread(_cleanup_superseded)
    clear_list_agents_cache()
    state.push_refresh("agents")
    result: dict[str, object] = {"ok": True, "template": new_name, "filename": dest.name}
    if publish_warning:
        result["warning"] = publish_warning
    return web.json_response(result)


async def api_agent_detail(request: web.Request) -> web.Response:
    """GET/DELETE/PATCH /api/agents/detail/{name} — view, delete, or update agent config."""
    name = request.match_info["name"]
    if request.method != "GET":
        denied = await _require_owner(request, f"agent_detail.{request.method.lower()}")
        if denied is not None:
            return denied
    # Parse body early so a malformed body returns 400, not 404 from the file loop.
    patch_body = None
    if request.method == "PATCH":
        try:
            patch_body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # Valid JSON is not necessarily an object. A top-level array makes
        # ``"skills" in patch_body`` a LIST-membership test (true for
        # ``["skills"]``), and the subscript that follows then raises TypeError
        # -> HTTP 500. Reject the shape once, here, rather than per-field.
        if not isinstance(patch_body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

    state: DashboardState = request.app["state"]
    for f in kiro_agents_dir_path().glob("*.json"):
        spec = _read_agent_spec(
            f,
            operation="api_agent_detail",
            source="dashboard",
        )
        if spec is None:
            continue
        # Two-step so ``data`` stays typed ``dict`` for the PATCH branch's
        # re-read below, which reassigns it from a raw ``json.loads``.
        data = spec
        # The try stays even though the parse moved out: the DELETE/PATCH
        # bodies still raise the caught pair mid-flight (the PATCH re-read
        # under the config lock, unlink), and those were -- and remain --
        # skip-to-next-file.
        try:
            if data.get("name") == name or f.stem == name:
                if request.method == "DELETE":
                    if f.name in (
                        "kirocrew.json",
                        "kirocrew-lite.json",
                    ):
                        return web.json_response({"error": "cannot delete kirocrew"}, status=400)
                    # The dashboard withholds delete for a template that is the
                    # kiro-cli fallback or is bound to a crew, but a client-side
                    # check cannot be race-free: its view of the config is a
                    # cached snapshot, so a crew bound (or the fallback moved)
                    # between render and click still reaches this handler. Only
                    # a check that reads config UNDER THE SAME LOCK the writers
                    # take makes the invariant hold, so this is the authority
                    # and the UI is now just the early, friendlier signal.
                    async with _get_config_lock():
                        # Off-thread: this runs while the config lock is HELD, so a
                        # synchronous read stalls the event loop AND queues every
                        # other config writer behind a disk read. `to_thread` is the
                        # idiom already used for this in core.py / files.py.
                        cfg = await asyncio.to_thread(KiroCrewConfig.load)
                        # Every identifier this file answers to. The match above
                        # accepts EITHER the JSON's own "name" or the filename
                        # stem, so a request naming one leaves the other out of
                        # `name` -- and config may well record the one omitted.
                        #
                        # `declared` goes through `spec_str`, the helper this file
                        # already uses on the GET path: `~/.kiro/agents` is a
                        # SHARED directory and a structured `name` (an ACP-style
                        # `{"id": ...}`) is observed in the wild. It is reachable
                        # here via the `f.stem == name` arm of the match, and an
                        # unhashable set member turns this into a TypeError --
                        # which the `except (JSONDecodeError, OSError)` below does
                        # NOT catch, so it escapes as HTTP 500 on a delete that
                        # should have succeeded or returned a 409.
                        declared = spec_str(data, "name")
                        aliases = {name, f.stem}
                        if declared:
                            aliases.add(declared)
                        # Both fields are checked because the two readers disagree:
                        # `agent.default_agent` is the kiro-cli fallback, while
                        # top-level `default_agent` is what /api/config/default-agent
                        # (the picker on this very page) writes. Guarding only one
                        # leaves the other free to dangle.
                        if aliases & {cfg.agent.default_agent, cfg.default_agent}:
                            return web.json_response(
                                {
                                    "error": (
                                        f"Cannot delete '{name}': it is the default agent used "
                                        "when nothing names a template. Change the default first."
                                    ),
                                    "code": "agent_is_default",
                                },
                                status=409,
                            )
                        bound = sorted(
                            crew
                            for crew, crew_cfg in cfg.agents.items()
                            if crew_cfg.kiro_agent in aliases
                        )
                        if bound:
                            return web.json_response(
                                {
                                    "error": (
                                        f"Cannot delete '{name}': still used by "
                                        f"{', '.join(bound)}. Repoint or remove them first."
                                    ),
                                    "code": "agent_in_use",
                                },
                                status=409,
                            )

                        # Unlink + prune in ONE spec-lock hold, off the loop: an
                        # unlocked unlink can land inside the fork refresh's
                        # read-modify-write, which then re-creates the file —
                        # with lineage pruned, the private copy surfaces as a
                        # shared template. (The unlink is also a blocking
                        # syscall that must not run on the event loop.)
                        def _locked_delete() -> None:
                            with agents_spec_lock(f.parent):
                                f.unlink()
                                with contextlib.suppress(Exception):
                                    agent_state.prune(declared or name)

                        await asyncio.to_thread(_locked_delete)
                    clear_list_agents_cache()
                    state.push_refresh("agents")
                    return web.json_response({"ok": True})
                if request.method == "PATCH" and patch_body is not None:
                    if "skills" in patch_body:
                        raw_skills = patch_body["skills"]
                        if not isinstance(raw_skills, list) or not all(
                            isinstance(s, str) for s in raw_skills
                        ):
                            return web.json_response(
                                {"error": "skills must be a list of strings"}, status=400
                            )
                        if len(raw_skills) > MAX_AGENT_SKILLS:
                            return web.json_response(
                                {"error": f"at most {MAX_AGENT_SKILLS} skills per agent"},
                                status=400,
                            )
                    mapped: list[str] = []
                    loop = asyncio.get_running_loop()
                    async with _get_config_lock():
                        # Re-read under the lock: the copy above was read before
                        # the lock and a concurrent PATCH may have superseded it.
                        # The branch writes this data back, so bind the same
                        # agents directory and apply the stricter no-symlink /
                        # no-escape fence before the hardened read.  Keep the
                        # filesystem work off the event loop while the shared
                        # config lock is held.
                        agents_dir = kiro_agents_dir_path()

                        def _reread_under_lock(
                            spec_file: Path = f,
                            root: Path = agents_dir,
                        ) -> dict[str, Any] | None:
                            if not _spec_path_is_safe(spec_file, root):
                                return None
                            return _read_agent_spec(
                                spec_file,
                                operation="api_agent_detail",
                                source="dashboard",
                            )

                        reread_data = await asyncio.to_thread(_reread_under_lock)
                        if reread_data is None:
                            return web.json_response(
                                {
                                    "error": f"'{name}' changed on disk during update; retry.",
                                    "code": "agent_changed",
                                },
                                status=409,
                            )
                        data = reread_data
                        # Pristine snapshot: the locked write below re-reads the
                        # CURRENT disk state and re-applies only the keys this
                        # PATCH changed relative to this snapshot, so it cannot
                        # clobber a concurrent refresh's writes with stale data.
                        before_patch = copy.deepcopy(reread_data)
                        # `spec_str` for the same reason as `declared` above: a
                        # hand-edited spec can carry a structured (non-string)
                        # "name", which would crash the sidecar helper's dict
                        # lookup with an unhashable key.
                        agent_name = spec_str(data, "name") or name
                        # Skills FIRST, before any state mutation. The mapping can
                        # reject the request (unknown key -> 400) and the model
                        # branch below writes the agent_state sidecar; doing model
                        # first meant a rejected combined PATCH still froze the
                        # model against future shipped-default bumps.
                        #
                        # Offloaded to the discovery pool: the mapping enumerates
                        # the skill roots (see enumerate_skill_catalog), which on a
                        # large or network-backed catalog is enough filesystem work
                        # to stall the event loop — the same reason /api/skills and
                        # /api/agents/installed run off the loop.
                        if "skills" in patch_body:
                            mapped, unknown = await loop.run_in_executor(
                                discovery_executor(),
                                apply_skill_mapping,
                                data,
                                f,
                                state,
                                list(patch_body["skills"]),
                                _read_session_key(request),
                            )
                            if unknown:
                                return web.json_response(
                                    {"error": "unknown skills", "skills": unknown[:20]},
                                    status=400,
                                )
                        else:
                            mapped = await loop.run_in_executor(
                                discovery_executor(),
                                agent_skill_keys,
                                data,
                                f,
                                state,
                                _read_session_key(request),
                            )
                        if "model" in patch_body:
                            # Stored verbatim (canonical key); translated to a
                            # provider id at the config.loader factory boundary.
                            data["model"] = patch_body["model"] or None
                            if data["model"] is None:
                                # Cleared/auto: resume tracking the shipped
                                # default (re-synced by _refresh_dynamic_fields).
                                # Shared with `kirocrew agent reset-model` so the
                                # two surfaces cannot disagree on what clearing a
                                # model means. Offloaded: it writes the sidecar
                                # under the blocking cross-process lock.
                                await asyncio.to_thread(clear_model_pin, data, agent_name)
                            else:
                                # Explicit pick: freeze it against default bumps.
                                await asyncio.to_thread(
                                    agent_state.set_model_managed, agent_name, False
                                )
                        # Never persist Kiro Crew bookkeeping into the kiro spec —
                        # kiro-cli rejects unknown fields and drops the agent. Same
                        # shared helper as the PUT handler and migrate_agent_specs(),
                        # so this fourth writer can't drift from the other three.
                        # The model branch above may have just set the
                        # sidecar explicitly; the helper only lifts a stale key out
                        # of `data` when the sidecar is still unset, so it can't
                        # clobber that just-written value. Offloaded like the PUT
                        # handler: the helper does synchronous sidecar read/write
                        # filesystem work that would stall the event loop.
                        await asyncio.to_thread(
                            agent_state.lift_and_strip_bookkeeping, data, agent_name
                        )

                        def _locked_overwrite() -> None:
                            # Same spec lock as fork/publish and the background
                            # fork refresh — and a full read-merge-write inside
                            # it: our `data` snapshot was taken before the lock,
                            # so a concurrent refresh may have sanitized away a
                            # ceiling-rejected grant since; writing the snapshot
                            # verbatim would restore it. Merge only the keys THIS
                            # patch changed onto the fresh read, then run the
                            # mandated whole-config governance funnel immediately
                            # before persisting (same contract as
                            # _write_spec_file and the PUT handler).
                            with agents_spec_lock(f.parent):
                                fresh = _read_agent_spec(
                                    f, operation="api_agent_detail", source="dashboard"
                                )
                                if fresh is None:
                                    raise FileNotFoundError(f)
                                for key, value in data.items():
                                    if key not in before_patch or before_patch[key] != value:
                                        fresh[key] = value
                                for key in before_patch:
                                    if key not in data:
                                        fresh.pop(key, None)
                                sanitize_agent_config_governance(fresh)
                                # Atomic replace: a direct write truncates first,
                                # so ENOSPC mid-write would destroy the existing
                                # template. Same tmp+rename helper as the fork
                                # refresh and install paths.
                                _atomic_json_write(f, fresh)

                        try:
                            await asyncio.to_thread(_locked_overwrite)
                        except FileNotFoundError:
                            return web.json_response(
                                {
                                    "error": f"'{name}' changed on disk during update; retry.",
                                    "code": "agent_changed",
                                },
                                status=409,
                            )
                    # The list_agents() cache keys on a (count, newest-mtime-ns)
                    # signature; two writes inside the same mtime granularity
                    # would otherwise serve a stale skill list.
                    clear_list_agents_cache()
                    state.push_refresh("agents")
                    return web.json_response(
                        {"ok": True, "model": data.get("model", ""), "skills": mapped}
                    )
                # ``skills`` / ``unmanaged_skills`` are computed, response-only
                # views of ``resources`` — never written back into the spec
                # (kiro-cli rejects unknown fields and drops the agent). One
                # catalog walk for both, off the event loop (filesystem-heavy).
                keys, unmanaged_uris = await asyncio.get_running_loop().run_in_executor(
                    discovery_executor(),
                    agent_skill_views,
                    data,
                    f,
                    state,
                    _read_session_key(request),
                )
                return web.json_response(
                    {
                        **data,
                        # The rest of the spec is passed through verbatim, but
                        # these two are CONSUMED as display text by the detail
                        # panel. A foreign spec's structured value rendered as a
                        # React child throws error #31 and blanks the whole tab,
                        # so both are coerced on the same "non-string means
                        # absent" rule list_agents() uses.
                        "description": spec_str(data, "description"),
                        "model": spec_model(data),
                        "skills": keys,
                        "unmanaged_skills": unmanaged_uris,
                    }
                )
        except (json.JSONDecodeError, OSError):
            continue
    # "default" is the built-in agent with no config file
    if name == "default":
        if request.method != "GET":
            return web.json_response({"error": "cannot modify built-in default agent"}, status=400)
        return web.json_response({"name": "default", "model": ""})

    return web.json_response({"error": "not found"}, status=404)


async def api_capability_agents_list(request: web.Request) -> web.Response:
    """GET /api/capability/agents — list installed agent packages (edition capability manager)."""
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response(await mgr.list_agents())
    except Exception as exc:
        return _err500(exc)


async def api_capability_mcp_registry(request: web.Request) -> web.Response:
    """GET /api/capability/mcp/registry — browse available MCP servers from the registry.

    The capability manager owns registry-output parsing and returns entries
    directly (conventional keys: id, installed, title, tier, description); the
    core passes them through verbatim.
    """
    mgr = _capability_manager()
    if not mgr.available():
        return web.json_response({"error": _CAPABILITY_UNAVAILABLE}, status=503)
    try:
        return web.json_response({"servers": await mgr.registry()})
    except Exception as exc:
        return _err500(exc)


# ── KiroCrew Agent CRUD API ──


def _roster_mask(value: object) -> str:
    """Render ONE agent-record value for a roster row, masking what cannot be shown.

    Every record value is agent- or package-writable: an agent can edit
    ``config.json`` directly, and ``_do_agents_sync`` copies ``description``
    straight off a discovered agent spec, so a third-party package controls that
    string. A value the redactors would alter -- credential- or
    exfiltration-URL-shaped text -- is therefore replaced WHOLESALE by
    ``_SENSITIVE_MASK``, the sentinel ``_masked_config_dict`` already uses for
    the same job on ``GET /api/config/kirocrew``. A non-string (the loader lets
    an object through five declared-``str`` fields) is masked too: it is not
    renderable, so there is nothing to show. Benign content is byte-identical.

    **A fixed sentinel rather than redacting in place, and that is the whole
    design.** An in-place scrub makes the browser's view a FUNCTION of the
    stored value, so the write-side rule that keeps a read-modify-write from
    persisting that view (``_carries_mask``) has to recognise it by
    recomputing the transform -- which breaks in two ways a sentinel does not:

    * **Redaction-chain drift.** This same response is also wrapped in
      ``redact_record_strings``, whose order differs from ``_redact_external``'s.
      A recomputed-equality rule would stop matching and silently persist the
      redacted text; an exact sentinel survives, because scrubbing
      ``_SENSITIVE_MASK`` leaves it unchanged.
    * **Stale-view skew.** If the stored value changes between the GET and the
      PUT (an agent editing ``config.json``, a second dashboard tab), a
      recomputed rule compares the old view against the NEW value, fails to
      match, and writes ``[REDACTED ...]`` text into the config as though the
      operator had typed it. The sentinel does not depend on the stored value at
      all, so this cannot happen.

    Named cost: a value containing one credential-shaped token is masked
    entirely, so the owner loses the benign remainder of that string rather than
    seeing it partially redacted. That is the same trade ``_masked_config_dict``
    already makes, and it is the price of a view that cannot be mistaken for
    content.
    """
    # Function-local for the reason recorded at the ``_validate_role_model``
    # import below: ``handlers.core`` resolves ``_get_config_lock`` from THIS
    # module, so a module-level import here would close the cycle.
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    if not isinstance(value, str):
        return _SENSITIVE_MASK
    return value if _redact_external(value) == value else _SENSITIVE_MASK


def _carries_mask(incoming: object) -> bool:
    """True when *incoming* still CARRIES the mask, so it is not real content.

    The write-side half of ``_roster_mask``. A client that read a roster row and
    echoed it back sends the mask; persisting it would destroy the operator's
    stored value. Such a field is treated as UNCHANGED instead.

    **Containment, not equality.** An exact-match rule closes only the
    echo-it-back case. The editor renders the mask into a text input, so an
    operator who APPENDS to it submits ``"<mask> and also X"`` -- not equal to the
    sentinel, so an equality rule would persist the redaction glyphs plus the
    addition, replacing the stored original. Any string still containing the
    sentinel is therefore refused as content.

    Consequence, stated because it is a real limitation and not a free win: a
    genuine replacement must OMIT the sentinel entirely -- clear the field, then
    type the new value. An edit that keeps the mask and adds to it is dropped
    rather than half-applied. That is lossy in the operator's INTENT, but it never
    destroys what is stored, and the alternative writes redaction glyphs into
    ``config.json`` over the real value.

    This is the remedy ``_masked_config_dict``'s docstring prescribes -- "MUST
    treat ``_SENSITIVE_MASK`` as 'unchanged' and keep the stored value" -- read
    the strict way. Because the comparison is against a FIXED sentinel and never
    against a recomputation of the stored value, it is immune to which redaction
    chain produced the view and to the stored value having changed since the read.

    Accepted residual, identical in kind to the config endpoint's: an operator
    cannot store a value containing the mask string. It is eight U+2022 bullets.

    **Recursive, because one shipped field is STRUCTURED.** ``avatar`` is a dict
    whose ``traits`` values are masked (``_roster_avatar``), so an echoed avatar
    carries the sentinel one level DOWN. A top-level-only check sees a ``dict``,
    answers "not a mask", and lets ``_safe_avatar`` persist the sentinel over the
    stored trait -- the exact corruption this predicate exists to prevent, one
    level deeper than the flat fields. Any string anywhere inside the value
    therefore counts.

    Cost of the recursive form, stated because it is sharper than the flat one: if
    an avatar carries ANY masked trait, the whole avatar field is treated as
    unchanged, so an edit to a DIFFERENT trait in the same avatar is dropped too.
    A partial merge would be the alternative, and it is worse: it would have to
    decide field-by-field which half of a structured value is authoritative, and
    getting that wrong writes glyphs into stored config. Refusing the whole field
    never destroys what is stored.
    """
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    if isinstance(incoming, str):
        return _SENSITIVE_MASK in incoming
    if isinstance(incoming, dict):
        return any(_carries_mask(val) for val in incoming.values())
    if isinstance(incoming, (list, tuple)):
        return any(_carries_mask(val) for val in incoming)
    return False


def _roster_avatar(value: object) -> dict:
    """The one STRUCTURED field a roster row ships: shape-allowlisted, not masked.

    ``avatar`` is a ``dict``, so ``_roster_mask``'s non-string rule would blank it
    wholesale -- and the dashboard needs it: ``AgentSelector.tsx`` declares
    ``avatar?: unknown`` commented "verbatim from the backend", and ``main``'s
    roster still ships it via the ``asdict`` spread this PR replaces. Withholding
    it would REGRESS a live feature rather than narrow a disclosure, so it is
    named in the allowlist like every other shipped field.

    **A shape allowlist, with masking confined to the leaves that can carry user
    text.** ``_safe_avatar`` is the config's own validator, so only
    ``{"kind": "ghost", "traits": {...}}``, ``{"kind": "image", "v": ...,
    "file": "<digest>.<ext>"}`` and ``{"kind": "pack", "id": "<pack id>"}`` survive
    and junk collapses to ``{}``. Within that,
    ONLY ``traits`` values are masked:

    - ``kind`` and ``v`` are structural. Mask ``kind`` and the dashboard can no
      longer tell a ghost from an uploaded picture.
    - ``file`` is PINNED by ``_AVATAR_FILE_PIN_RE`` to ``<16-hex>.<ext>``, so it is
      not arbitrary text. A value constrained by a regex is safer than a masked
      one: the pin REFUSES a bad value where masking would destroy a good one, and
      a masked ``file`` makes the per-crew avatar endpoint resolve nothing --
      silently breaking the image.
    - ``traits`` values, and the ``eyes``/``mouth`` values of each
      ``expressions`` state, are the only user-authored strings here, so they go
      through ``_roster_mask`` like any other roster string. The renderer resolves
      an unrecognized trait to absent (``EYES[k] ?? ''``), so a masked trait
      degrades that axis rather than breaking the face.
    - ``sounds`` values are constrained by ``_safe_sounds`` to a shipped preset
      name, so they are pinned rather than masked -- the same reason ``file`` is.
    - ``id`` (on ``kind: "pack"``) is pinned by
      ``appearance_packs.safe_pack_id`` to letters, digits, dash and underscore,
      so it is not arbitrary text either — and masking it would make the pack
      routes resolve nothing, silently blanking the face for the same reason a
      masked ``file`` breaks the image.

    Honest limit on how far the two rules can be told apart: because
    ``_safe_avatar`` already pins every non-``traits`` leaf to a shape the redactors
    do not alter (a literal ``kind``, a digest ``file``, a hex ``tile``), a blanket
    mask over the validated dict would behave the SAME as this targeted one today.
    The targeted rule is chosen for intent and for the day that pin loosens, not
    because a live defect separates them -- and no test can pin the difference
    while the shape validator holds.

    No host path is disclosed by any of this. The picture's bytes live under the
    data home's agent-fenced ``run/avatars/`` dir and are served by the per-crew
    avatar endpoint; the config field only marks the choice.
    """
    safe = _safe_avatar(value)
    traits = safe.get("traits")
    if isinstance(traits, dict):
        safe = dict(safe)
        safe["traits"] = {
            axis: (_roster_mask(val) if isinstance(val, str) else val)
            for axis, val in traits.items()
        }
    expressions = safe.get("expressions")
    if isinstance(expressions, dict):
        safe = dict(safe)
        safe["expressions"] = {
            state: {
                axis: (_roster_mask(val) if isinstance(val, str) else val)
                for axis, val in axes.items()
            }
            for state, axes in expressions.items()
            if isinstance(axes, dict)
        }
    return safe


def _name_would_be_masked(name: str) -> bool:
    """True when *name* is credential-shaped, so a roster row would mask it.

    Keyed on ``_roster_mask`` itself rather than on a second detector, so the
    create-time rule and the read-time rule cannot drift apart: a name that would
    arrive masked is a name that can never be stored in the first place.
    """
    return _roster_mask(name) != name


def _agent_roster_row(
    name: str, scope: str, agent_cfg: KiroCrewAgentConfig, *, redact: bool
) -> dict[str, object]:
    """Serialize ONE ``GET /api/agents`` roster row.

    **Key half.** Explicit allowlist -- never a ``dataclasses.asdict`` spread,
    mirroring the rule ``handlers/members.py`` already documents for
    ``GET /api/members``. The response is a network-boundary contract, and a
    spread makes that contract "every field ``KiroCrewAgentConfig`` has now, plus
    every field anyone adds later", automatically -- so a field added by someone
    who never looked at this endpoint (internal bookkeeping, a filesystem path, a
    capability hint, a credential-shaped one) ships to the browser by omission.
    Naming each field inverts the default: nothing leaves unless it is added here
    deliberately. Both row sources go through this one function, so the
    ``cfg.agents`` rows and the project-scope rows cannot drift into different
    key sets.

    **Value half.** Every record value goes through ``_roster_mask``, for every
    caller, uniformly -- see there for why they are all untrusted and why the
    mask is a fixed sentinel. ``_carries_mask`` is its write-side half in
    ``api_kirocrew_agent_update``; neither is correct alone, and an end-to-end
    test does the GET then the PUT to prove the pair.

    Uniform rather than per-field on purpose: exempting the fields the agents
    page happens to write back would encode a claim about the CLIENT that this
    side cannot enforce -- and a false one, because
    ``api_kirocrew_agent_update`` accepts ``description`` and ``source`` too.

    ``name`` is the single exception, and only for the owner: it is the row's
    IDENTITY, addressing ``/api/agents/{name}`` for edit and delete and keying
    the usage sort, and it travels in the URL rather than the body so the
    write-side rule cannot protect it. Masking it would make the row
    unaddressable. An ``app`` token cannot reach those owner-gated routes, so the
    exemption buys it nothing and ``name`` is masked there. A credential-shaped
    name is refused at CREATION (``_name_would_be_masked``), closing the hazard
    at its source rather than at this one read site -- but only for names arriving
    through that route, so an already-stored one still reaches here and is still
    masked for every caller but the owner. Named cost: an app that feeds a roster
    name to another route sees the mask, which happens only for a name containing
    credential- or URL-shaped text.

    ``scope`` is never masked: it is a literal written here, not record content.
    The annotation is ``dict[str, object]`` rather than ``dict[str, str]`` because
    of ONE field: ``avatar`` is a structured ``dict`` the dashboard needs verbatim
    (see ``_roster_avatar``). Every other value is a ``str`` -- ``_roster_mask``
    returns one for every input, including the non-strings the loader lets
    through.

    Excluded on purpose, each verified to have NO consumer in ``website/src``:
    ``watchdog_tool_stall_suspect_secs`` and ``watchdog_tool_stall_hard_cap_secs``
    (per-agent watchdog windows -- backend scheduling knobs the roster does not
    render) and ``telegram_account`` (deprecated and inert, and the one record
    field naming an external messaging binding). Adding any of them back is a
    one-line change plus the pinned key set.
    """
    return {
        # ``name`` is masked for an app token (which can address nothing) and for
        # every PROJECT row (which nothing can address either: both
        # ``api_kirocrew_agent_update`` and ``api_kirocrew_agent_delete`` 404 on a
        # name absent from ``cfg.agents``, and a scanned project agent never is).
        # A GLOBAL row's name survives for a non-app caller because it is that
        # row's only handle -- it addresses ``/api/agents/{name}`` for edit and
        # delete and keys the usage sort -- and masking it there would buy
        # nothing: the same names are readable unmasked from
        # ``GET /api/config/kirocrew``, where they are the ``agents`` map's KEYS
        # and ``_masked_config_dict`` masks only schema-``sensitive`` VALUES.
        # That last argument does NOT extend to project rows, whose names come
        # from a filesystem scan and appear in no config, which is why they are
        # masked here rather than reasoned away.
        #
        # Named cost: a project agent whose FILENAME is credential- or
        # URL-shaped is not selectable, because the picker dispatches by
        # this value (``AgentSelector.tsx:127`` ``onChange(a.name)``). That is
        # confined to names the redactors would alter; an ordinary project agent
        # name is byte-identical.
        "name": _roster_mask(name) if (redact or scope == "project") else name,
        # The project-scope tag: "project" rows dispatch only from the
        # slot whose project they were scanned from. Handler-added, not a
        # record field.
        "scope": scope,
        "kiro_agent": _roster_mask(agent_cfg.kiro_agent),
        "workspace": _roster_mask(agent_cfg.workspace),
        "memory_store": _roster_mask(agent_cfg.memory_store),
        "model": _roster_mask(agent_cfg.model),
        "reasoning_effort": _roster_mask(agent_cfg.reasoning_effort),
        "description": _roster_mask(agent_cfg.description),
        "triggers": _roster_mask(agent_cfg.triggers),
        "source": _roster_mask(agent_cfg.source),
        "session_color": _roster_mask(agent_cfg.session_color),
        # The one STRUCTURED value a row carries -- shape-allowlisted by
        # ``_safe_avatar`` with masking confined to user-authored ``traits``
        # values, so ``kind``/``v``/``file`` survive as the pinned shapes the
        # dashboard and the per-crew avatar endpoint need. See ``_roster_avatar``.
        "avatar": _roster_avatar(getattr(agent_cfg, "avatar", {})),
    }


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
    # the mutating routes in this module, so the read and write sides now agree
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


async def api_kirocrew_agents_sync(request: web.Request) -> web.Response:
    """POST /api/agents/sync — auto-sync AIM-installed agents into config.json."""
    denied = await _require_owner(request, "agents.sync")
    if denied is not None:
        return denied
    async with _get_config_lock():
        return await _do_agents_sync(request)


async def _do_agents_sync(request: web.Request) -> web.Response:

    cfg = KiroCrewConfig.load()
    synced: list[str] = []
    pruned: list[str] = []
    prune_candidates: dict[str, dict] = {}
    prior_stores: dict[str, str] = {}
    try:
        discovered_agents = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), lambda: list(list_agents())
        )
        discovered_names = {a.name for a in discovered_agents}

        # Add new agents
        mc_kiro_agents = {a.kiro_agent for a in cfg.agents.values()}
        for disc in discovered_agents:
            if (
                disc.name not in mc_kiro_agents
                and disc.name not in cfg.agents
                and disc.source != "kirocrew"
                # A fork is one crew's private copy, not a standalone template:
                # normally its owner's binding puts it in mc_kiro_agents, so this
                # only fires for an ORPHANED copy (owner crew deleted) — which
                # must not resurrect as a ghost agent.
                and not disc.private_to
            ):
                # EXECUTABLE INVARIANT enforcement (mirrors the seam-boundary
                # LIVENESS bound in platform.capability_bound —
                # BoundedCapabilityManager): a builtin_agents() row MUST be
                # spawnable. The core can only verify the on-disk case
                # (~/.kiro/agents/<name>.json); an edition may also make a
                # row ACP-resolvable WITHOUT an on-disk file, so we WARN rather
                # than hard-drop — dropping a legitimately ACP-resolvable agent
                # would itself be a correctness bug. The warning turns an
                # otherwise silent spawn-time (ACP session/set_mode) failure into
                # an actionable log line pointing at the offending seam row.
                # Upstream resolves the agents dir per call (data-home safety);
                # the namespaced check is for app-provided agents, which live as
                # `<app>--<agent>.json` and would otherwise look "missing".
                # Off the loop: both the stat and the namespaced glob touch the
                # filesystem, and on a populated agents directory this per-agent
                # check (in a loop) would stall the gateway loop and heartbeat.
                _dn = disc.name
                # The OTHER way a name reaches `cfg.agents`, and the one the
                # create-route check cannot see. A discovered spec's name is
                # package-controlled rather than typed by the owner, so "the owner
                # is reading a string the owner wrote" does not hold for it: a
                # package could land a credential-shaped name that then reaches
                # the roster. Refused here, at the second source, for the same
                # reason it is refused at the first.
                #
                # Skipped rather than masked: masking would leave an
                # unselectable, unrenameable row, and this row has no owner to
                # rename it -- it comes back on every sync until the PACKAGE is
                # fixed. The name is deliberately absent from the log line, since
                # writing it into the log is the disclosure being avoided.
                if _name_would_be_masked(_dn):
                    logger.warning(
                        "refusing to sync a discovered agent whose name is "
                        "credential- or URL-shaped (source=%s); name withheld "
                        "from this log deliberately -- fix the providing package",
                        getattr(disc, "source", "?"),
                    )
                    continue
                await _drained_to_thread(require_member_memory_creation, disc.name)
                _has_on_disk = await asyncio.to_thread(
                    lambda: (kiro_agents_dir_path() / f"{_dn}.json").exists()
                    or _namespaced_agent_file_exists(_dn)
                )
                if not _has_on_disk:
                    logger.warning(
                        "syncing agent %r (source=%s) with no on-disk config at "
                        "%s — if it is not ACP-resolvable it will persist into "
                        "config.json and fail at spawn (builtin_agents EXECUTABLE "
                        "INVARIANT)",
                        disc.name,
                        disc.source,
                        kiro_agents_dir_path() / f"{disc.name}.json",
                    )
                cfg.agents[disc.name] = KiroCrewAgentConfig(
                    kiro_agent=disc.name,
                    description=disc.description,
                    source=disc.source,
                )
                prior_stores[disc.name] = cfg.agents[disc.name].memory_store
                await _drained_to_thread(provision_member_memory, cfg, disc.name)
                synced.append(disc.name)

        # Prune agents whose kiro_agent file no longer exists on disk.
        # Only prune package-installed agents (never user-created or kirocrew-owned).
        # Skip pruning if scan returned nothing -- likely a transient issue.
        # Invariant: for package-sourced entries, kiro_agent == dict key == agent name.
        # ("aim" is also accepted for backward-compat with older configs.)
        # A STARRED package crew is pruned like any other -- a registry row with
        # no spec on disk is not spawnable. The star goes with the row: a
        # reinstalled crew comes back un-starred and one click restores it
        # (deliberately no parking list -- a permanent config key is not worth
        # a re-click, and a name-keyed list would pre-star an unrelated future
        # package that reused the name).
        if discovered_names:
            for name, agent_cfg in list(cfg.agents.items()):
                if agent_cfg.source in ("package", "aim") and (
                    agent_cfg.kiro_agent not in discovered_names
                ):
                    # Record the SNAPSHOT entry: the locked mutate below only
                    # prunes a name whose in-lock entry still equals this one,
                    # so an agent (re)added by a newer sync between this
                    # snapshot and the lock is never deleted on stale evidence.
                    prune_candidates[name] = dataclasses.asdict(agent_cfg)
                    del cfg.agents[name]
                    pruned.append(name)
    except BaseException as exc:
        await _retire_failed_member_allocations(cfg, prior_stores)
        if isinstance(exc, UnknownMemoryStore):
            return web.json_response(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "member_memory_unavailable",
                    "synced": [],
                },
                status=409,
            )
        if not isinstance(exc, Exception):
            raise
        logger.warning("Failed to scan installed agents", exc_info=True)
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="failure",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync failure", exc_info=True)
        return web.json_response({"ok": False, "error": "sync failed", "synced": []}, status=500)

    if synced or pruned:
        try:
            # The caller (api_kirocrew_agents_sync) holds _get_config_lock().
            # Persist as a DELTA read-modify-write inside a single sidecar-
            # flock hold: the adds and prunes decided on the snapshot
            # above are re-applied to the document as read inside the lock,
            # so a concurrent writer's unrelated settings are untouchable --
            # a whole-document save() would publish the stale snapshot over
            # them. _drained_to_thread so a cancellation cannot release the
            # asyncio lock while the worker is mid-write.
            to_add = {n: cfg.agents[n] for n in synced if n in cfg.agents}
            to_add_stores = {
                n: (
                    cfg.agents[n].memory_store,
                    dataclasses.asdict(cfg.memory_stores[cfg.agents[n].memory_store]),
                )
                for n in to_add
            }

            def _write_sync() -> list[str]:
                retired_stores: list[str] = []
                created_archives: list[tuple[str, str]] = []
                skipped_allocations: list[tuple[str, str]] = []

                def _mutate(doc: dict) -> dict | None:
                    agents = coerce_dict_section(doc, "agents")
                    stores = coerce_dict_section(doc, "memory_stores")
                    changed = False
                    for aname, acfg in to_add.items():
                        if aname not in agents:
                            store_name, store_record = to_add_stores[aname]
                            existing = stores.get(store_name)
                            if existing is not None and existing != store_record:
                                raise UnknownMemoryStore(
                                    f"memory store {store_name!r} ownership changed concurrently"
                                )
                            require_member_memory_not_archived(store_name, expected_owner=aname)
                            stores[store_name] = store_record
                            agents[aname] = dataclasses.asdict(acfg)
                            changed = True
                        else:
                            skipped_allocations.append((to_add_stores[aname][0], aname))
                    # Prune ONLY this sync's snapshot candidates, and only
                    # while the in-lock entry still equals the snapshot entry:
                    # an agent (re)added or edited between the discovery
                    # snapshot and this lock hold is newer evidence than the
                    # stale discovered_names and must survive.
                    for aname, snap_entry in prune_candidates.items():
                        if agents.get(aname) == snap_entry:
                            store_name = snap_entry.get("memory_store", "")
                            record = stores.get(store_name)
                            if isinstance(record, dict) and record.get("memory_version") == 2:
                                owner = record.get("owner_member")
                                if owner != aname:
                                    raise UnknownMemoryStore(
                                        f"memory store {store_name!r} ownership changed concurrently"
                                    )
                                if archive_member_memory_store(store_name, aname):
                                    created_archives.append((store_name, aname))
                                retired_stores.append(store_name)
                            del agents[aname]
                            changed = True
                    return doc if changed else None

                try:
                    update_config_locked(mutate=_mutate)
                except BaseException:
                    for store_name, owner in reversed(created_archives):
                        try:
                            rollback_member_memory_archive_if_active(store_name, owner)
                        except Exception:
                            logger.error(
                                "failed to roll back member memory retirement for %s",
                                store_name,
                                exc_info=True,
                            )
                    raise
                from kiro_crew.context import release_cached_memory_store

                for store_name, owner in skipped_allocations:
                    retire_unpublished_member_memory_store(store_name, owner)
                for store_name in retired_stores:
                    release_cached_memory_store(store_name)
                return retired_stores

            retired_stores = await _drained_to_thread(_write_sync)
            if (state := request.app.get("state")) is not None:
                from kiro_crew.dashboard.handlers._shared import release_markdown_memory_store

                for store_name in retired_stores:
                    await release_markdown_memory_store(state, store_name)
        except BaseException as exc:
            await _retire_failed_member_allocations(cfg, prior_stores)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Failed to save config after agent sync", exc_info=True)
            try:
                _sel().log_api_access(
                    caller=request.get("user", "dashboard"),
                    operation="agent.auto_sync",
                    outcome="failure",
                    source="agent_sync",
                )
            except Exception:
                logger.warning("SEL logging failed for config save failure", exc_info=True)
            return web.json_response(
                {"ok": False, "error": "config save failed", "synced": []}, status=500
            )
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="success",
                source="agent_sync",
                resources=", ".join(synced + [f"-{p}" for p in pruned]),
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync success", exc_info=True)
    else:
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="noop",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync noop", exc_info=True)

    if pruned:
        logger.info("Pruned %d stale package agents: %s", len(pruned), ", ".join(pruned))

    return web.json_response({"ok": True, "synced": synced, "pruned": pruned})


async def _retire_failed_member_allocations(
    config: KiroCrewConfig, prior_stores: dict[str, str]
) -> None:
    """Preserve the primary failure while retiring only this operation's new keys."""
    for owner, prior in prior_stores.items():
        try:
            store = config.agents[owner].memory_store
            if store == prior:
                continue
            await _drained_to_thread(retire_unpublished_member_memory_store, store, owner)
        except BaseException:
            logger.warning(
                "Could not retire unpublished memory for member %s", owner, exc_info=True
            )


async def api_kirocrew_agent_resolved_model(request: web.Request) -> web.Response:
    """GET /api/agents/resolved-model?agent=NAME — the model a new session uses.

    Serves the one backend resolver so the dashboard's model chip does not have
    to re-derive the precedence client-side (and drift from it). ``agent`` is a
    KiroCrew agent name; omitted falls back to the configured default agent.
    ``model`` is "" when every tier defers to the backend's own choice.
    """
    agent_name = request.query.get("agent", "").strip()
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # Globs ~/.kiro/agents and may read the installed agent file — keep the
    # filesystem work off the event loop.
    model = await asyncio.to_thread(resolve_effective_model, cfg, agent_name or None)
    alias, kiro_agent, model_pin = await asyncio.to_thread(
        resolve_agent_identity, cfg, agent_name or None
    )
    # Effort resolves through its own chain, served from the SAME resolver the
    # provider factory calls so the pane cannot disagree with what a session will
    # actually run at -- including the role-aware default, which a crew bound to a
    # background worker agent takes instead of the chat default. Keyed on what the
    # bindings resolved, which is what makes an omitted `agent` answer for the
    # configured default crew rather than for no crew at all.
    crew_effort = cfg.crew_pinned_effort(None, alias)
    session_effort = cfg.resolve_session_effort(kiro_agent, alias)
    return web.json_response(
        {
            "model": model,
            "agent": agent_name,
            "kiro_agent": kiro_agent,
            # Whether the agent itself pins the model, vs inheriting it.
            "pinned": bool(model_pin),
            # The effort a new session on this crew starts at, and whether the
            # crew pinned it or inherited a default. "" means no tier pins one
            # and the model's own default applies.
            "reasoning_effort": session_effort,
            "effort_pinned": bool(crew_effort),
        }
    )


def _effort_inputs(crew: KiroCrewAgentConfig | None) -> tuple[str, str] | None:
    """The crew fields `resolve_session_effort` reads, or ``None`` for no crew.

    Derived from the resolver's inputs rather than enumerated per call site. The
    chain reads two things off the record -- the pin itself, and the bound
    ``kiro_agent`` the role default keys on -- and the factory answers from the
    config it captured, so ANY change to either (including a crew appearing or
    disappearing) must invalidate that capture. Three rounds of review found the
    per-condition version incomplete one case at a time (an unpinned crew whose
    binding makes it a background worker; a re-bound `kiro_agent`); comparing this
    tuple before and after a write is the invariant those cases are instances of,
    and a future field the chain starts reading is added here once instead of at
    every handler.
    """
    if crew is None:
        return None
    return (crew.kiro_agent, coerce_effort(crew.reasoning_effort))


async def _refresh_session_defaults(request: web.Request, crew: str) -> None:
    """Rebuild the provider factory so a crew's new effort pin reaches new sessions.

    The factory resolves the pin from the config it captured when it was built, so
    a write alone stays invisible until a restart. That is the same staleness
    ``api_kirocrew_config_patch`` already handles for ``agent.reasoning_effort``
    and ``agent.role_efforts.*``, and for the same reason it uses
    ``refresh_defaults()`` rather than ``reload_provider_factory()``: the factory
    is rebuilt and the warm pool drained WITHOUT touching live sessions, so an
    in-flight turn is not killed because a default changed. The pool must drain
    either way -- a pre-warmed child carries the old effort overlay, and the claim
    path never re-pushes effort.

    Best-effort: the write is already durable, so a crew save must not fail
    because the refresh did. A failure costs one gateway lifetime of staleness.
    """
    state = request.app.get("state")
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return
    try:
        await sessions.refresh_defaults()
    except Exception:
        logger.warning(
            "Could not refresh session defaults after saving crew %r; the pin "
            "applies from the next gateway start",
            crew,
            exc_info=True,
        )


def _crew_effort_rejected(raw: object) -> str | None:
    """Reason a crew's reasoning-effort pin is unusable, or ``None`` to allow it.

    Rejects rather than coercing, which is the opposite of the config-file load
    path (:func:`coerce_effort`). The difference is who is watching: a typo in a
    hand-edited file must not stop the gateway from booting, but a typo sent by
    the crew form has an author on the other end, and silently storing ``""``
    would read back as "inherits" and look like the save was lost.

    ``""`` is the inherit sentinel and always allowed -- it is how a pin is
    cleared.
    """
    if not isinstance(raw, str):
        return "reasoning_effort must be a string"
    val = raw.strip()
    if val in EFFORT_VALUES:
        return None
    return "reasoning_effort must be one of: " + ", ".join(("(empty)", *EFFORT_LEVELS))


def _crew_memory_store_rejected(raw: object) -> str | None:
    """Reason a crew's memory-store binding is unusable, or ``None`` to allow it.

    The rules themselves are ``memory_stores``' and are never restated here: a
    second copy of the shape rule is how the write boundary comes to accept a name
    the resolvers refuse to compose a path for, and that refusal would then surface
    at the crew's first memory write rather than on the form that authored it.

    Rejects rather than degrading, for the same reason as
    :func:`_crew_effort_rejected`: the value has an author on the other end, and a
    name quietly degraded onto another crew's silo reads back as a save that was
    lost while the crew files its memory somewhere it was never bound.

    This helper checks only shape. The create and update handlers separately
    enforce automatic allocation and immutable private ownership.
    """
    defect = memory_store_binding_defect(raw)
    if defect is None:
        return None
    return (
        f"memory_store {raw!r} is not a usable store name ({defect}); use lowercase "
        "letters, digits and hyphens, or '' for automatic provisioning on creation"
    )


def _model_pin_rejected(model: str, request: web.Request, provider: str) -> str | None:
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

    return _validate_role_model(model, request, provider=provider)


async def api_kirocrew_agents_create(request: web.Request) -> web.Response:
    """POST /api/agents — create a new KiroCrew agent."""

    denied = await _require_owner(request, "agent.create")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "Agent name is required"}, status=400)
    # Refused at the SOURCE, not masked at one read site. Once such a name is
    # stored it reaches logs, error messages, telemetry and every other surface
    # that prints a crew name -- none of which this module controls -- so closing
    # it here closes it once, where masking a read closes one of N. Keyed on
    # ``_roster_mask`` via ``_name_would_be_masked``, so this rule and the
    # roster's cannot drift apart.
    #
    # BOUNDARY, stated because it is real and narrower than "the hazard is
    # closed": this covers only names created THROUGH this route, from now on. A
    # crew already present in `config.json`, one written there by hand, and one
    # added by ``_do_agents_sync`` from a discovered spec are NOT retroactively
    # renamed. That is the reason the owner keeps reading a stored name verbatim:
    # renaming is the remediation, and a name must be legible to be renamed.
    #
    # The name is deliberately NOT echoed back. Reflecting a credential-shaped
    # string into a response body -- and from there into the request log -- is the
    # disclosure this rule exists to prevent.
    if _name_would_be_masked(name):
        return web.json_response(
            {
                "error": (
                    "Agent name looks like a credential or a URL carrying one. "
                    "Pick a name that identifies the crew instead."
                ),
                "code": "credential_shaped_name",
            },
            status=400,
        )
    # The template pointer must be EXPLICIT. Defaulting it to "kirocrew" would
    # make every crew created without naming a template an alias for the DEFAULT
    # agent: dispatch flattens an alias to its `kiro_agent`
    # (config.loader.resolve_agent_bindings), so the crew is offered in the chat
    # picker and then the default answers. "kirocrew" is a perfectly valid CHOICE
    # here (a crew booting the built-in agent against its own workspace/memory
    # store is the common case); only the silent default is refused.
    kiro_agent = str(body.get("kiro_agent") or "").strip()
    if not kiro_agent:
        return web.json_response(
            {
                "error": "kiro_agent is required — name the agent this crew boots "
                "from (pass 'kirocrew' for the built-in agent)",
                "code": "kiro_agent_required",
            },
            status=400,
        )
    # Grammar-checked before the name is persisted or used to look anything up.
    # This is the one shared agent-name grammar every other boundary uses, so a
    # value that cannot name an agent (path separators, traversal, wildcards,
    # over-length) is refused here rather than stored as a dangling pointer.
    if not _AGENT_NAME_RE.match(kiro_agent):
        return web.json_response(
            {"error": "invalid kiro_agent name", "code": "invalid_kiro_agent_name"},
            status=400,
        )
    # Existence is resolved through `list_agents()`, which reads every spec via the
    # hardened reader: it resolves symlinks, refuses a spec whose REAL target is
    # sensitive, and goes through the same gate as every other dashboard file read.
    # A direct filename probe here called `Path.read_text()` itself, so a namespaced
    # agent file symlinked at a credentials path would have been read outside that
    # gate. `list_agents()` is also the broader and more accurate notion of
    # existence: it includes edition-provided rows that are ACP-resolvable with no
    # on-disk file, which is what "will this actually dispatch" means.
    # Off the loop: it scans and parses the agent directories.
    known_agents = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), lambda: {a.name for a in list_agents()}
    )
    template_missing = kiro_agent not in known_agents
    # Unknown-but-accepted: an edition may resolve a row this listing cannot see,
    # so refusing here would break a legitimate crew. WARN instead — the same
    # posture, and for the same reason, as the sync path's EXECUTABLE INVARIANT
    # check — so a crew that will fail at spawn leaves a trace rather than
    # failing silently later.
    if template_missing:
        logger.warning(
            "creating crew %r against template %r, which is not in the installed "
            "agent listing — if it is not ACP-resolvable the crew will fail at spawn",
            name,
            kiro_agent,
        )
    # Passed RAW, not str()-coerced: normalize_agent_model is total and maps a
    # non-string to "" (inherit). Wrapping in str() first would turn
    # {"model": 123} into the literal "123", which normalizes to a string the
    # backend then rejects as an unknown model id.
    model = normalize_agent_model(body.get("model"))
    _raw_color = body.get("session_color", "")
    session_color = _safe_color(_raw_color)
    if _raw_color not in ("", None) and not session_color:
        return web.json_response(
            {"error": "session_color must be #rrggbb or empty", "code": "invalid_color_hex"},
            status=400,
        )
    _raw_effort = body.get("reasoning_effort", "")
    effort_reason = _crew_effort_rejected(_raw_effort)
    if effort_reason:
        return web.json_response(
            {"error": effort_reason, "code": "invalid_reasoning_effort"}, status=400
        )
    reasoning_effort = _raw_effort.strip()
    # Same convention as session_color: a non-empty raw value that the coercer
    # collapses to "no override" is a caller mistake worth a 400, not a silent
    # fallback to the name-derived face. The one exception is a well-formed
    # ghost override whose traits all coerce to absent: that collapse is the
    # validator's own all-empty→reset rule, not caller junk, so it stores as
    # the canonical reset rather than being refused.
    _raw_avatar = body.get("avatar")
    avatar = _safe_avatar(_raw_avatar)
    if _raw_avatar not in (None, {}) and not avatar and not _is_ghost_shaped(_raw_avatar):
        return web.json_response(
            {
                "error": "avatar must be {'kind': 'ghost', 'traits': {...}}, {'kind': 'image'}, {'kind': 'pack', 'id': ...}, or empty",
                "code": "invalid_avatar",
            },
            status=400,
        )
    if avatar.get("kind") == "image":
        # A crew that does not exist yet cannot have staged a picture (the
        # upload endpoint 404s for unknown names), so an image override on
        # create can never have a file to commit.
        return web.json_response(
            {
                "error": "upload the picture after creating the crew",
                "code": "avatar_file_missing",
            },
            status=400,
        )
    # Old clients still send default/empty on create. They now mean automatic
    # private allocation; no caller can choose or reuse another member's store.
    memory_store = body.get("memory_store", DEFAULT_MEMORY_STORE)
    memory_store_reason = _crew_memory_store_rejected(memory_store)
    if memory_store_reason:
        return web.json_response(
            {"error": memory_store_reason, "code": "invalid_memory_store"}, status=400
        )
    if memory_store not in ("", DEFAULT_MEMORY_STORE):
        return web.json_response(
            {
                "error": "A new Crew Member receives its own empty private memory automatically",
                "code": "private_memory_required",
            },
            status=400,
        )
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)
        model_reason = _model_pin_rejected(model, request, cfg.agent.provider)
        if model_reason:
            return web.json_response({"error": model_reason, "code": "invalid_model"}, status=400)
        # Checked INSIDE the config lock, immediately before the binding is
        # added: a fork recording lineage after a pre-lock validation must not
        # slip another crew's private copy into this new binding (GPT
        # round-34, same shape as the locked rebind's in-mutate check).
        try:
            owner = await asyncio.to_thread(_foreign_private_copy_owner, name, kiro_agent)
        except _UnverifiableLineage:
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{kiro_agent}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        if owner:
            return web.json_response(
                {
                    "error": f"Template '{kiro_agent}' is crew '{owner}'s private copy; "
                    "it cannot be bound to another crew.",
                    "code": "foreign_private_copy",
                },
                status=409,
            )
        new_agent = KiroCrewAgentConfig(
            kiro_agent=kiro_agent,
            workspace=body.get("workspace", "default"),
            memory_store=memory_store,
            model=model,
            reasoning_effort=reasoning_effort,
            description=body.get("description", ""),
            triggers=body.get("triggers", ""),
            source=body.get("source", "kirocrew"),
            session_color=session_color,
            avatar=avatar,
        )
        # Provision against this snapshot; publish the agent and owned store
        # together through persist_member_config's flocked delta and create guard.
        cfg.agents[name] = new_agent
        try:
            try:
                await _drained_to_thread(require_member_memory_creation, name)
                await _drained_to_thread(provision_member_memory, cfg, name)
                await _drained_to_thread(lambda: persist_member_config(cfg, name, create=True))
            except BaseException:
                await _retire_failed_member_allocations(cfg, {name: memory_store})
                raise
        except MemberAlreadyExists:
            return web.json_response(
                {"error": f"Agent '{name}' already exists", "code": "agent_exists"}, status=409
            )
        except (OSError, UnknownMemoryStore) as exc:
            return web.json_response(
                {"error": str(exc), "code": "member_memory_unavailable"}, status=409
            )
    # A crew APPEARING changes what the effort chain resolves even with no pin of
    # its own: the factory's captured config does not know the crew, so it cannot
    # read the binding the role default keys on, and a scheduled or messaging
    # session naming that crew would take the chat default instead. Creation is
    # therefore always a change by `_effort_inputs` (None -> a tuple).
    await _refresh_session_defaults(request, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response(
        {"ok": True, "name": name, "memory_store": cfg.agents[name].memory_store}
    )


async def _retire_legacy_member_contexts(
    request: web.Request, cfg: KiroCrewConfig, name: str, prior_store: str
) -> web.Response | None:
    """Retire idle V1 providers while retaining their original conversation identity."""
    from kiro_crew.dashboard.chat_utils import effective_session_key, subagents_attached

    state = request.app.get("state")
    if state is None:
        return None
    slots = [
        slot
        for slot in list(state._slots.values())
        if slot.agent == name or (not slot.agent and cfg.default_agent == name)
    ]
    for slot in slots:
        async with slot._lock:
            key = effective_session_key(slot)
            if state._slots.get(slot.key) is not slot:
                continue
            if slot.agent != name and not (not slot.agent and cfg.default_agent == name):
                continue
            provider = state.sessions.get_provider(key)
            if (
                slot.running
                or slot._in_stage_execution
                or (provider is not None and provider.has_active_turn())
                or subagents_attached(state, slot, key, "member_memory_opt_in")
            ):
                return web.json_response(
                    {
                        "error": "Finish or stop this member's work before creating private memory",
                        "code": "member_memory_busy",
                    },
                    status=409,
                )
            # This V1 context must remain V1 even if publication is cancelled,
            # or another request starts while later slots are being retired.
            slot.memory_store = prior_store
            slot._memory_assignment_from_history = True
            eager = slot._eager_spawn_task
            if eager is not None and not eager.done():
                eager.cancel()
                await asyncio.gather(eager, return_exceptions=True)
            reset = await state.sessions.reset(key, skip_if_busy=True)
            if not reset and state.sessions.get_provider(key) is not None:
                return web.json_response(
                    {
                        "error": "A member turn started during memory setup; retry after it stops",
                        "code": "member_memory_busy",
                    },
                    status=409,
                )
    return None


async def api_kirocrew_agent_update(request: web.Request) -> web.Response:
    """PUT /api/agents/{name} — update a KiroCrew agent."""

    denied = await _require_owner(request, "agent.update")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    # WRITE-SIDE HALF of the roster mask, and it runs FIRST -- immediately after
    # the body-object check, before any validation. `GET /api/agents` replaces a
    # value it cannot show verbatim with `_SENSITIVE_MASK` (`_roster_mask`), and
    # a client that echoes the record back -- the agents page sends every field
    # on every save, so that `""` can clear a pin -- would otherwise persist the
    # mask over the stored original. A field carrying the mask therefore means
    # "unchanged" and is dropped here, which is the remedy
    # `_masked_config_dict`'s docstring prescribes verbatim: "MUST treat
    # `_SENSITIVE_MASK` as 'unchanged' and keep the stored value".
    #
    # Ordering is load-bearing, not cosmetic: `model` and `reasoning_effort` are
    # validated below and would REJECT an echoed mask with a 400, failing an edit
    # to some unrelated field. Dropping the masked entries before those checks
    # means a mask can never be validated as if it were content.
    #
    # It can run this early only because the predicate matches a FIXED sentinel
    # and needs no access to the stored record -- a rule that recognised the view
    # by recomputing the redaction of `agent` would have to wait for the config
    # load inside the lock, and would therefore sit after these validations.
    body = {key: val for key, val in body.items() if not _carries_mask(val)}
    # Binding-only fast path (the template pane's saved-as-you-go switch). The
    # generic path below writes a full ``cfg.save()`` snapshot, which races
    # every other config writer (CLI, settings PUTs) and silently reverts
    # their concurrent changes — so a payload that moves ONLY the binding goes
    # through the locked binding-delta writer instead, stale-checked against
    # the caller's expected prior binding when it supplies one.
    if "kiro_agent" in body and set(body) <= {"kiro_agent", "expected_kiro_agent"}:
        new_target = body["kiro_agent"]
        if not isinstance(new_target, str) or not new_target:
            return web.json_response(
                {"error": "kiro_agent must be a non-empty string", "code": "invalid_kiro_agent"},
                status=400,
            )
        expected_raw = body.get("expected_kiro_agent")
        if expected_raw is not None and not isinstance(expected_raw, str):
            return web.json_response(
                {
                    "error": "expected_kiro_agent must be a string",
                    "code": "invalid_expected_kiro_agent",
                },
                status=400,
            )
        current = await asyncio.to_thread(KiroCrewConfig.load)
        if name not in current.agents:
            return web.json_response(
                {"error": f"Agent '{name}' not found", "code": "agent_not_found"}, status=404
            )
        # The new target itself stays acceptable so a repeated switch to the
        # same template is idempotent rather than a spurious conflict.
        expected = None if expected_raw is None else (expected_raw, new_target)
        # Under the handler-level config lock, like fork/publish/reset: the
        # cross-process advisory lock inside ``_rebind_crew_locked`` guards the
        # file write, but it cannot stop the generic path below from saving a
        # full snapshot it loaded BEFORE this rebind (it holds this lock across
        # its load->save span and awaits in between). Without taking the same
        # lock here, that stale snapshot lands after the delta and silently
        # reverts the switch while both requests report success.
        try:
            async with _get_config_lock():
                await asyncio.to_thread(_rebind_crew_locked, name, expected, new_target)
        except _StaleBinding:
            return web.json_response(
                {
                    "error": "The crew's template changed underneath this switch; reload and retry.",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _ForeignPrivateCopy as exc:
            return web.json_response(
                {
                    "error": f"Template '{new_target}' is crew '{exc.owner}'s private copy; "
                    "it cannot be bound to another crew.",
                    "code": "foreign_private_copy",
                },
                status=409,
            )
        except _UnverifiableLineage:
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{new_target}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        # A binding change moves what the effort chain resolves, same as the
        # generic path.
        await _refresh_session_defaults(request, name)
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent.update",
            outcome="success",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"ok": True, "name": name})
    if "model" in body:
        pending_model = normalize_agent_model(body["model"])
    # Rejected before the config is even loaded: the check is pure, and every
    # validation must land before the first field assignment below so a bad value
    # cannot leave the in-memory record half-updated.
    if "reasoning_effort" in body:
        effort_reason = _crew_effort_rejected(body["reasoning_effort"])
        if effort_reason:
            return web.json_response(
                {"error": effort_reason, "code": "invalid_reasoning_effort"}, status=400
            )
    # Same placement rule as reasoning_effort: validated up here, before the
    # lock and before any field or avatar-file mutation. A body that pairs a
    # bad `starred` with an avatar promotion would otherwise move the staged
    # picture and then 400 without rolling it back. Strictly a bool: a string
    # "false" from a hand-typed request must not read as truthy and star the crew.
    if "starred" in body and not isinstance(body["starred"], bool):
        return web.json_response(
            {"error": "starred must be a boolean", "code": "invalid_starred"}, status=400
        )
    if "memory_store" in body:
        memory_store_reason = _crew_memory_store_rejected(body["memory_store"])
        if memory_store_reason:
            return web.json_response(
                {"error": memory_store_reason, "code": "invalid_memory_store"}, status=400
            )
    if "provision_memory" in body and not isinstance(body["provision_memory"], bool):
        return web.json_response(
            {"error": "provision_memory must be a boolean", "code": "invalid_provision_memory"},
            status=400,
        )
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if "model" in body:
            # Validated before the write, reusing the config loaded just above so
            # this costs no extra read.
            model_reason = _model_pin_rejected(pending_model, request, cfg.agent.provider)
            if model_reason:
                return web.json_response(
                    {"error": model_reason, "code": "invalid_model"}, status=400
                )
        agent = cfg.agents[name]
        prior_memory_store = agent.memory_store
        if "memory_store" in body and body["memory_store"] != prior_memory_store:
            return web.json_response(
                {
                    "error": "A member's private memory cannot be rebound or shared",
                    "code": "private_memory_immutable",
                },
                status=409,
            )
        prior_record = cfg.memory_stores.get(prior_memory_store)
        if body.get("provision_memory") and (
            prior_record is None or prior_record.memory_version != 2
        ):
            try:
                await _drained_to_thread(require_member_memory_creation, name)
            except UnknownMemoryStore as exc:
                return web.json_response(
                    {"error": str(exc), "code": "member_memory_unavailable"}, status=409
                )
        # Captured BEFORE any mutation: what the effort chain reads today.
        effort_inputs_before = _effort_inputs(agent)
        changed: list[str] = []
        if "kiro_agent" in body:
            try:
                owner = await asyncio.to_thread(
                    _foreign_private_copy_owner, name, body["kiro_agent"]
                )
            except _UnverifiableLineage:
                return web.json_response(
                    {
                        "error": f"Cannot verify whether '{body['kiro_agent']}' is a "
                        "private copy; retry.",
                        "code": "lineage_unverifiable",
                    },
                    status=409,
                )
            if owner:
                return web.json_response(
                    {
                        "error": f"Template '{body['kiro_agent']}' is crew '{owner}'s "
                        "private copy; it cannot be bound to another crew.",
                        "code": "foreign_private_copy",
                    },
                    status=409,
                )
            agent.kiro_agent = body["kiro_agent"]
            changed.append("kiro_agent")
        if "workspace" in body:
            agent.workspace = body["workspace"]
            changed.append("workspace")
        if "model" in body:
            # "auto"/"" both mean inherit; store the single "" spelling so the
            # agent keeps deferring to the kiro pin / global fallback. Raw, not
            # str()-coerced — see the create path for why.
            agent.model = normalize_agent_model(body["model"])
            changed.append("model")
        if "reasoning_effort" in body:
            # Already validated above; "" is the inherit sentinel and clears a pin.
            agent.reasoning_effort = body["reasoning_effort"].strip()
            changed.append("reasoning_effort")
        if "description" in body:
            agent.description = body["description"]
            changed.append("description")
        if "triggers" in body:
            agent.triggers = body["triggers"]
            changed.append("triggers")
        if "session_color" in body:
            _sc = body["session_color"]
            _norm = _safe_color(_sc)
            if _sc not in ("", None) and not _norm:
                return web.json_response(
                    {
                        "error": "session_color must be #rrggbb or empty",
                        "code": "invalid_color_hex",
                    },
                    status=400,
                )
            agent.session_color = _norm
            changed.append("session_color")
        _avatar_promoted = False
        _avatar_pin = ""
        _prior_pin: object = None
        _remove_files_after_save = False
        if "avatar" in body:
            _raw_av = body["avatar"]
            _av = _safe_avatar(_raw_av)
            # Same 400 convention as session_color: junk that coerces to "no
            # override" is refused rather than silently clearing the face.
            # None/{} are the explicit "reset to name-derived" spellings, and
            # a well-formed ghost override that collapses all-empty is the
            # validator's own reset rule, not caller junk.
            if _raw_av not in (None, {}) and not _av and not _is_ghost_shaped(_raw_av):
                return web.json_response(
                    {
                        "error": "avatar must be {'kind': 'ghost', 'traits': {...}}, {'kind': 'image'}, {'kind': 'pack', 'id': ...}, or empty",
                        "code": "invalid_avatar",
                    },
                    status=400,
                )
            _av = _carry_pack_through_faceless_save(agent.avatar, _raw_av, _av)
            if _av.get("kind") == "image":
                # THE commit point for pictures, under this same config lock.
                # `promote` is a wire-only directive (never persisted — the
                # validator drops it): the client sets it exactly when THIS
                # save staged a fresh upload. Without it, a leftover staging
                # from an earlier failed or abandoned save must NOT ride along
                # into an unrelated edit — it is discarded instead, and the
                # crew keeps wearing its current picture.
                _tok = _raw_av.get("token") if isinstance(_raw_av, dict) else None
                _wants_promote = isinstance(_raw_av, dict) and _raw_av.get("promote") is True
                _prior_pin = agent.avatar.get("file")
                if _wants_promote and not isinstance(_tok, str):
                    # `promote` without its staging token must not slide into
                    # the picture-keeping branch: that would discard the
                    # staged replacement and report success for a save that
                    # installed nothing.
                    return web.json_response(
                        {
                            "error": "promote requires the staging token from the upload",
                            "code": "avatar_file_missing",
                        },
                        status=400,
                    )
                if _wants_promote and isinstance(_tok, str):
                    _promoted = await _drained_to_thread(_promote_pending_avatar, name, _tok)
                    _avatar_promoted = _promoted is not None
                    if _promoted is None:
                        # The bytes THIS save staged are gone (a newer save
                        # re-staged the slot, or staging was cleaned up).
                        # Falling back to the current live picture would
                        # report success while silently dropping the user's
                        # selected replacement — fail the commit instead.
                        return web.json_response(
                            {
                                "error": "staged avatar no longer matches this save"
                                " — upload the picture again",
                                "code": "avatar_file_missing",
                            },
                            status=400,
                        )
                    stamp, _avatar_pin = _promoted
                else:
                    await _drained_to_thread(_discard_pending_avatar, name)
                    # A picture-keeping edit (no fresh upload): stamp from the
                    # file the config's pin already selects.
                    _live = await asyncio.to_thread(_live_avatar_file, name, _prior_pin)
                    stamp = None
                    if _live is not None:
                        _avatar_pin = _live.name[len(_avatar_stem(name)) + 1 :]
                        try:
                            stamp = int((await asyncio.to_thread(_live.stat)).st_mtime_ns)
                        except OSError:
                            stamp = None
                if stamp is None:
                    return web.json_response(
                        {
                            "error": "no uploaded avatar file to commit — POST the picture first",
                            "code": "avatar_file_missing",
                        },
                        status=400,
                    )
                # Rebuilt, not mutated, so the record carries exactly the
                # committed stamp and pin. The per-state keys are validated
                # input rather than commit output, so they have to be carried
                # across explicitly -- otherwise saving a sound on a crew that
                # wears a picture reports success and stores nothing.
                _av = {
                    "kind": "image",
                    "v": stamp,
                    "file": _avatar_pin,
                    **{k: v for k, v in _av.items() if k in ("expressions", "sounds")},
                }
            elif agent.avatar.get("kind") == "image":
                # Leaving the picture tier: the stored file must not linger
                # as a silently-retrievable orphan — but only once the config
                # write that stops selecting it has actually succeeded.
                _remove_files_after_save = True
            agent.avatar = _av
            changed.append("avatar")
        if "source" in body:
            agent.source = body["source"]
            changed.append("source")
        if "starred" in body:
            # Already validated above, before any mutation.
            agent.starred = body["starred"]
            changed.append("starred")
        # Validate every supplied field before allocating private ownership.
        # A rejected edit must not leave private evidence behind an unchanged
        # V1 binding. Avatar publication is reversible until config is saved.
        setup_refusal = None
        try:
            try:
                if body.get("provision_memory"):
                    prior_record = cfg.memory_stores.get(prior_memory_store)
                    if prior_record is None or prior_record.memory_version != 2:
                        from kiro_crew.memory_stores import require_member_memory_store

                        await _drained_to_thread(require_member_memory_store, cfg, name)
                        setup_refusal = await _retire_legacy_member_contexts(
                            request, cfg, name, prior_memory_store
                        )
                    if setup_refusal is None:
                        await _drained_to_thread(provision_member_memory, cfg, name)
            except BaseException:
                await _retire_failed_member_allocations(cfg, {name: prior_memory_store})
                raise
        except (OSError, UnknownMemoryStore) as exc:
            setup_refusal = web.json_response(
                {"error": str(exc), "code": "member_memory_unavailable"}, status=409
            )
        if setup_refusal is not None:
            if _avatar_promoted:
                await _drained_to_thread(_rollback_promoted_avatar, name, _avatar_pin, _prior_pin)
            return setup_refusal
        if agent.memory_store != prior_memory_store:
            changed.append("memory_store")
        effort_inputs_after = _effort_inputs(agent)
        # Avatar rollback applies only to ordinary failure: cancellation may
        # arrive after the drained worker published the new avatar pin. Store
        # cleanup independently checks the current locked config, so a landed
        # memory binding survives even when the request was cancelled.
        try:
            await _drained_to_thread(
                lambda: persist_member_config(
                    cfg, name, expected_store=prior_memory_store, changed_fields=set(changed)
                )
            )
        except BaseException as exc:
            await _retire_failed_member_allocations(cfg, {name: prior_memory_store})
            if isinstance(exc, Exception) and _avatar_promoted:
                await _drained_to_thread(_rollback_promoted_avatar, name, _avatar_pin, _prior_pin)
            raise
        if _avatar_promoted:
            await _drained_to_thread(_commit_promoted_avatar, name, _avatar_pin)
        if _remove_files_after_save:
            await _drained_to_thread(_remove_avatar_files, name)
    # Compared, not merely "the body carried the field": the crew form sends
    # reasoning_effort on every save (that is what makes clearing a pin possible)
    # and refresh_defaults drains the warm pool, so refreshing on presence would
    # cost a cold start on every unrelated crew edit. A re-bound kiro_agent counts
    # too -- the role default reads it. Outside the config lock, because the
    # refresh takes the session locks.
    if effort_inputs_after != effort_inputs_before:
        await _refresh_session_defaults(request, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.update",
        outcome="success",
        source="dashboard",
        resources=f"{name} ({','.join(changed)})",
    )
    result = {"ok": True, "name": name, "memory_store": agent.memory_store}
    if agent.memory_store != prior_memory_store:
        result["new_conversation_required"] = True
    return web.json_response(result)


async def api_kirocrew_agent_delete(request: web.Request) -> web.Response:
    """DELETE /api/agents/{name} — delete a KiroCrew agent."""

    denied = await _require_owner(request, "agent.delete")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if name == cfg.default_agent:
            return web.json_response(
                {"error": f"Cannot delete default agent '{name}'. Change default_agent first."},
                status=409,
            )
        created_archive = False
        retired_store = ""

        def _delete_member() -> tuple[str, bool]:
            nonlocal created_archive, retired_store

            def mutate(doc: dict) -> dict:
                nonlocal created_archive, retired_store
                agents = coerce_dict_section(doc, "agents")
                if name not in agents:
                    raise UnknownMemoryStore(f"Crew Member {name!r} was removed concurrently")
                agent_section = doc.get("agent")
                if doc.get("default_agent") == name or (
                    isinstance(agent_section, dict) and agent_section.get("default_agent") == name
                ):
                    raise UnknownMemoryStore(f"Crew Member {name!r} became the default")
                entry = agents[name]
                stores = coerce_dict_section(doc, "memory_stores")
                store_name = entry.get("memory_store", "") if isinstance(entry, dict) else ""
                record = stores.get(store_name)
                if isinstance(record, dict) and record.get("memory_version") == 2:
                    if record.get("owner_member") != name:
                        raise UnknownMemoryStore(
                            f"memory store {store_name!r} ownership changed concurrently"
                        )
                    created_archive = archive_member_memory_store(store_name, name)
                    retired_store = store_name
                del agents[name]
                return doc

            try:
                update_config_locked(mutate=mutate)
            except BaseException:
                if created_archive:
                    try:
                        rollback_member_memory_archive_if_active(retired_store, name)
                    except Exception:
                        logger.error(
                            "failed to roll back member memory retirement for %s",
                            retired_store,
                            exc_info=True,
                        )
                raise
            return retired_store, created_archive

        retired_store, _created_archive = await _drained_to_thread(_delete_member)
        if retired_store:
            from kiro_crew.context import release_cached_memory_store

            await _drained_to_thread(release_cached_memory_store, retired_store)
            if (state := request.app.get("state")) is not None:
                from kiro_crew.dashboard.handlers._shared import release_markdown_memory_store

                await release_markdown_memory_store(state, retired_store)
        # The crew is gone; its uploaded picture must not outlive it. Inside
        # the same lock so the cleanup cannot run AFTER a concurrent
        # same-name recreation has already uploaded and committed a new
        # picture under the same digest stem.
        await _drained_to_thread(_remove_avatar_files, name)
    # A crew DISAPPEARING is the other half of the same invariant: the captured
    # config still holds the record, so a cron or messaging job still naming the
    # crew would keep resolving its old pin and binding.
    await _refresh_session_defaults(request, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})


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


def _carry_pack_through_faceless_save(stored: dict, raw: object, validated: dict) -> dict:
    """Keep a worn pack when a save names no face at all.

    The shipped crew editor rebuilds the avatar from a CLOSED shape -- ghost or
    picture -- so for a crew wearing a pack it sees neither, renders the
    name-derived face, and on ANY unrelated save (a model change, a colour)
    submits ``{}`` or a faceless ``{"kind": "ghost", "sounds": ...}``. Taken at
    face value that is "reset", and the pack the user chose through the API is
    gone with no click that meant it. Until the picker can show a pack, a save
    that names no face therefore keeps the pack it found, and the reactions the
    save DID carry ride onto it -- so editing a sound on a pack-wearing crew
    stores the sound and keeps the pack.

    Deliberately narrow:

    * only when the CURRENT record is a pack -- ghost and picture keep their
      existing reset semantics untouched;
    * ``None`` on the wire is still an explicit reset (the editor never sends it,
      so it stays available to a caller that means it);
    * a real face -- a ghost with traits, a picture, another pack -- replaces the
      pack exactly as before.
    """
    if stored.get("kind") != "pack" or raw is None:
        return validated
    kind = validated.get("kind")
    if kind is None or (kind == "ghost" and "traits" not in validated):
        kept = {"kind": "pack", "id": stored["id"]}
        kept.update({k: v for k, v in validated.items() if k in ("expressions", "sounds")})
        return kept
    return validated


def _is_ghost_shaped(value: object) -> bool:
    """True when ``value`` is a structurally well-formed ghost override
    whose trait values all carry their schema types.

    Used to tell the validator's all-empty→reset collapse apart from caller
    junk at the 400 gate. Structure alone is not enough: the validator
    coerces a wrong-TYPE trait value (``{"eyes": 7}``) to absent, so a
    malformed payload would collapse to reset and — when the crew currently
    wears an uploaded picture — silently delete it. A payload only earns the
    reset collapse when every trait it names is validly typed (string axes
    are strings, boolean axes are real booleans), i.e. it is genuinely
    empty, not mistyped.
    """
    if not (
        isinstance(value, dict)
        and value.get("kind") == "ghost"
        and isinstance(value.get("traits"), dict)
    ):
        return False
    traits = value["traits"]
    known = set(_AVATAR_GHOST_STR_TRAITS) | set(_AVATAR_GHOST_BOOL_TRAITS) | {"tile"}
    if any(k not in known for k in traits):
        # An unknown axis name is a typo'd or version-skewed caller, not an
        # empty override — it must not earn the reset collapse.
        return False
    for key in _AVATAR_GHOST_STR_TRAITS:
        if key in traits and not isinstance(traits[key], str):
            return False
    for key in _AVATAR_GHOST_BOOL_TRAITS:
        if key in traits and not isinstance(traits[key], bool):
            return False
    tile = traits.get("tile", "")
    if not isinstance(tile, str):
        return False
    if tile and not _safe_color(tile):
        # A nonempty tile the color validator coerces to absent is junk,
        # not an intentionally empty axis.
        return False
    return True


def _avatars_dir() -> Path:
    """Uploaded-avatar directory, resolved against the live data home.

    Lives under ``run/`` — the data-home subtree the security layer fences
    from agent file tools (read AND write) — because the config's ``file``
    pin only proves which path was committed, not what is inside it: an
    agent that could write the pinned path would have its bytes served to
    the owner's authenticated dashboard as the saved picture. The gateway's
    own handlers open these paths directly in-process and do not route
    through that gate, so upload/serve/reap all work unchanged.

    Resolved per call, never captured at import — an import-time binding
    freezes the data home and defeats pod isolation and test isolation
    (dashboard/handlers/files.py is the precedent).
    """
    return data_home() / "run" / "avatars"


def _avatar_stem(name: str) -> str:
    """Path-safe filename stem for a crew's avatar.

    Crew names are display strings (spaces, CJK, anything) — a digest
    sidesteps every path-traversal and encoding question rather than
    answering them one by one. Full digest: truncating buys nothing and a
    shorter stem is the only thing a collision would need.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _avatar_variant_paths(name: str) -> list[Path]:
    """Every digest-named stored variant of ``name``'s picture on disk."""
    stem = _avatar_stem(name)
    d = _avatars_dir()
    out: list[Path] = []
    for p in d.glob(f"{stem}.*"):
        suffix = p.name[len(stem) + 1 :]
        if _AVATAR_FILE_PIN_RE.fullmatch(suffix) and p.is_file():
            out.append(p)
    return sorted(out)


def _pending_avatar_path(name: str) -> Path | None:
    """Return the STAGED (uploaded, not yet committed) file, or None."""
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        p = _avatars_dir() / f"{stem}.pending.{ext}"
        if p.is_file():
            return p
    return None


def _remove_avatar_files(name: str) -> None:
    """Delete every stored variant (installed + staged) of ``name``'s
    avatar, best-effort — a failed unlink is logged, never raised, because
    both callers (saving ``avatar: {}`` and crew deletion) have already
    cleared the config field, so the file is unreachable either way.
    """
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        try:
            (_avatars_dir() / f"{stem}.pending.{ext}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove staged avatar for %s (.%s)", name, ext)
    for p in _avatar_variant_paths(name):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove avatar file %s for %s", p.name, name)


def _discard_pending_avatar(name: str) -> None:
    """Remove any staged-but-uncommitted upload (best-effort)."""
    stem = _avatar_stem(name)
    for ext in _AVATAR_IMAGE_EXTS:
        try:
            (_avatars_dir() / f"{stem}.pending.{ext}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not discard pending avatar for %s (.%s)", name, ext)


def _read_avatar_file(path: Path) -> bytes | None:
    """Bounded, symlink-refusing read of a stored avatar file.

    The avatars dir sits behind the ``run/`` agent fence, but a stored file
    is still not trusted just because it is where an upload would have
    landed (defense in depth): a
    planted symlink must not let the authenticated GET read an arbitrary
    file, and a planted oversized blob must not be slurped unbounded into
    gateway memory. ``None`` means "treat as absent".
    """
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            logger.warning("refusing non-regular avatar file %s", path.name)
            return None
        with path.open("rb") as fh:
            data = fh.read(_AVATAR_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _AVATAR_MAX_BYTES:
        logger.warning("refusing oversized avatar file %s", path.name)
        return None
    return data


def _staging_token(data: bytes) -> str:
    """Identity of a staged upload: a content digest the PUT must echo.

    Staging is keyed by crew, so overlapping saves share the slot; the token
    is what stops save A's commit from promoting save B's bytes.
    """
    return hashlib.sha256(data).hexdigest()[:16]


def _promote_pending_avatar(name: str, token: str) -> tuple[int, str] | None:
    """Install the staged upload at its digest-named path; return
    ``(cache stamp, file pin)``.

    Installs only when the staged bytes match ``token`` (the digest the
    upload response handed THIS save) — a slot overwritten by a newer save's
    staging returns None instead of committing someone else's bytes. The
    install target is ``<stem>.<token>.<ext>``: content-addressed, so it can
    never collide with (or overwrite) the currently committed file — a
    process kill anywhere before the config save leaves the committed
    picture byte-identical at its own pinned path, and the orphaned install
    is reaped by the next successful commit. The caller MUST follow with
    :func:`_commit_promoted_avatar` (save succeeded) or
    :func:`_rollback_promoted_avatar` (save failed). Runs synchronous
    filesystem work: call via ``asyncio.to_thread``.
    """
    pending = _pending_avatar_path(name)
    if pending is None:
        return None
    staged = _read_avatar_file(pending)
    if staged is None or _staging_token(staged) != token:
        return None
    stem = _avatar_stem(name)
    d = _avatars_dir()
    pin = f"{token}{pending.suffix}"
    final = d / f"{stem}.{pin}"
    # replace_with_retry rides out the Windows sharing-violation window an
    # AV scanner or indexer opens on either path. Re-uploading bytes already
    # committed lands on the same digest path with identical content.
    replace_with_retry(pending, final)
    # Best-effort: a scanner holding a pending sibling open must not fail a
    # promotion whose install already landed.
    for other in _AVATAR_IMAGE_EXTS:
        try:
            (d / f"{stem}.pending.{other}").unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove staged avatar for %s (.%s)", name, other)
    # Nanosecond mtime: a same-size same-second replacement must still get a
    # fresh ?v= or the browser keeps showing the old bytes.
    return int(final.stat().st_mtime_ns), pin


def _commit_promoted_avatar(name: str, keep_pin: str) -> None:
    """After the config save succeeded: reap every variant except the one
    the config now pins — the previous picture and any orphaned installs.
    """
    for p in _avatar_variant_paths(name):
        if p.name[len(_avatar_stem(name)) + 1 :] == keep_pin:
            continue
        try:
            p.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not reap avatar variant %s for %s", p.name, name)


def _rollback_promoted_avatar(name: str, installed_pin: str, keep_pin: object) -> None:
    """Undo an install whose config save failed: remove the installed file.

    The committed picture was never touched — installs are content-addressed
    — so rollback is a single unlink, skipped when the install landed on the
    committed pin itself (a re-upload of identical bytes).
    """
    if installed_pin == keep_pin:
        return
    try:
        (_avatars_dir() / f"{_avatar_stem(name)}.{installed_pin}").unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove installed avatar %s for %s", installed_pin, name)


def _live_avatar_file(name: str, pin: object) -> Path | None:
    """The avatar file the config's ``file`` pin selects, or None.

    Only the exact pinned file counts. There is deliberately NO fallback for a
    record without a valid pin (a hand-edited ``{"kind": "image"}``): every
    writer stamps ``file`` at the commit, so a pinless record never names a
    committed picture, and "any stored variant" would include an orphaned
    install left by a crash between the install and the config save — the
    one file this pin exists to keep out of the roster. A pinless record
    therefore serves nothing (the frontend falls back to the seeded ghost)
    and a picture-keeping save on it fails with ``avatar_file_missing``
    rather than adopting an unknown file.
    """
    if isinstance(pin, str) and _AVATAR_FILE_PIN_RE.fullmatch(pin):
        p = _avatars_dir() / f"{_avatar_stem(name)}.{pin}"
        return p if p.is_file() else None
    return None


# ``asyncio.to_thread`` that a cancellation cannot abandon mid-mutation --
# moved to chat_utils so the files handler's staging copy can share the one
# implementation; the local name is kept for the call sites below.
_drained_to_thread = drained_to_thread


def _sniff_image_ext(head: bytes) -> str:
    """Return the format of ``head`` by magic bytes, or ``""``.

    PNG / JPEG / WEBP only — the formats every target browser renders in an
    ``<img>`` and none of which can carry active content the way SVG can.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def _image_body_complete(ext: str, body: bytes) -> bool:
    """Whether ``body`` is a structurally complete image of format ``ext``.

    Magic bytes alone accept a body cut off mid-stream (a client that died
    mid-upload, or a hand-built request), and committing one would reap the
    crew's previous picture in exchange for a file no browser can decode. A
    full decoder is not a dependency of this package, so this checks the one
    property every truncation breaks — that the container is closed:

    - PNG: the stream ends with the ``IEND`` chunk (its 4-byte CRC is fixed).
    - JPEG: the stream ends with the ``FFD9`` end-of-image marker.
    - WEBP: the RIFF header's declared payload length matches the body.

    Trailing padding after the terminator is not tolerated either: an
    ``<img>`` renders it fine, but it is exactly the shape a smuggled payload
    takes, and no encoder this endpoint accepts pictures from emits it.
    """
    if ext == "png":
        return body.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    if ext == "jpg":
        return body.endswith(b"\xff\xd9")
    if ext == "webp":
        if len(body) < 12:
            return False
        declared = int.from_bytes(body[4:8], "little")
        # RIFF length counts everything after the 8-byte RIFF header. A
        # single pad byte is legal when the payload length is odd.
        return declared + 8 in (len(body), len(body) - 1)
    return False


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
    etag = f'"{hashlib.sha256(data).hexdigest()[:32]}"'
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.Response(
        body=data,
        content_type=_AVATAR_CONTENT_TYPES[path.suffix.lstrip(".")],
        headers={"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"},
    )


async def api_kirocrew_agent_avatar_upload(request: web.Request) -> web.Response:
    """POST /api/agents/{name}/avatar — STAGE the crew's picture (multipart).

    Staging only: the file lands as ``<stem>.pending.<ext>`` and nothing the
    roster serves changes. The commit point is the ordinary agent update
    (`PUT /api/agents/{name}` with ``avatar: {"kind": "image"}``), which
    promotes the staged file and writes the field under one config lock —
    so a failed or abandoned Save can never have replaced the live picture,
    and the editor's Apply→Save two-step holds for images exactly as it
    does for ghost traits.
    """
    denied = await _require_owner(request, "agent.avatar_upload")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    if not (request.content_type or "").startswith("multipart/"):
        return web.json_response(
            {"error": "expected multipart/form-data", "code": "not_multipart"}, status=400
        )
    data = bytearray()
    try:
        reader = await request.multipart()
        part = await reader.next()
        # `next()` may yield a nested MultipartReader (multipart/mixed); only
        # a concrete body part carries a file, so anything else is skipped.
        while part is not None and (not isinstance(part, BodyPartReader) or part.name != "file"):
            part = await reader.next()
        if part is None:
            return web.json_response(
                {"error": "missing 'file' part", "code": "missing_file_part"}, status=400
            )
        while True:
            chunk = await part.read_chunk(64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _AVATAR_MAX_BYTES:
                return web.json_response(
                    {
                        "error": f"avatar exceeds {_AVATAR_MAX_BYTES // 1024} KB limit",
                        "code": "avatar_too_large",
                    },
                    status=413,
                )
    except (ValueError, AssertionError):
        # aiohttp raises plain ValueError for a bad/missing boundary or a
        # body truncated mid-part; that is caller junk, not a server error.
        return web.json_response(
            {"error": "malformed multipart body", "code": "invalid_multipart"}, status=400
        )
    ext = _sniff_image_ext(bytes(data[:16]))
    if not ext:
        return web.json_response(
            {
                "error": "avatar must be a PNG, JPEG, or WEBP image",
                "code": "avatar_bad_format",
            },
            status=400,
        )
    # Valid magic bytes on a truncated body must not stage: promotion would
    # reap the committed picture and serve an undecodable file in its place.
    if not _image_body_complete(ext, bytes(data)):
        return web.json_response(
            {
                "error": "avatar image is truncated or malformed — re-export and upload again",
                "code": "avatar_bad_format",
            },
            status=400,
        )

    def _stage() -> None:
        d = _avatars_dir()
        d.mkdir(parents=True, exist_ok=True)
        staged = d / f"{_avatar_stem(name)}.pending.{ext}"
        # Atomic even for the staging file: a crash mid-write must not leave
        # a truncated body a later promote would install. replace_with_retry
        # rides out the Windows sharing-violation window an AV scanner or
        # indexer opens on either path.
        tmp = staged.with_suffix(f".{ext}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_bytes(bytes(data))
            replace_with_retry(tmp, staged)
        finally:
            tmp.unlink(missing_ok=True)
        # A re-pick with a different format supersedes the previous staging.
        # Best-effort: a scanner holding a stale sibling open must not fail
        # the upload that already staged its bytes.
        for other in _AVATAR_IMAGE_EXTS:
            if other != ext:
                try:
                    (d / f"{_avatar_stem(name)}.pending.{other}").unlink(missing_ok=True)
                except OSError:
                    logger.debug("could not remove stale staging for %s (.%s)", name, other)

    # Staging happens under the config lock, with the crew's existence
    # re-checked inside it: an upload racing a crew deletion must not write
    # an orphan file the deletion's cleanup already missed. The multipart
    # body was fully read above, so the lock is held only for the short
    # filesystem commit.
    async with _get_config_lock():
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if name not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{name}' not found", "code": "agent_not_found"},
                status=404,
            )
        await _drained_to_thread(_stage)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.avatar_upload",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "staged": True, "token": _staging_token(bytes(data))})


# ── Conductor skill regeneration ────────────────────────────────────


def _regen_conductor() -> None:
    """Regenerate conductor skill after metadata or agent roster changes."""
    try:
        cfg = KiroCrewConfig.load()
        if not cfg.agent.conductor_skill:
            return
        from kiro_crew.conductor_skill import generate_conductor_skill  # noqa: F811
        from kiro_crew.skills import SkillsLoader  # noqa: F811

        generate_conductor_skill(SkillsLoader())
    except Exception:
        logger.exception("Failed to regenerate conductor skill")


async def api_agent_reset(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/reset — discard a crew's private copy.

    A server-side transaction replacing the panel's client-orchestrated
    rebind-then-delete: the client sequence could rebind to an origin deleted
    after panel load and then delete the copy, leaving the crew bound to
    nothing. Here the origin's existence is validated, the rebind runs under
    the config lock with a stale-binding check, and only a PERSISTED rebind
    is followed by the locked delete of the copy.
    """
    denied = await _require_owner(request, "agent_reset")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    crew = body.get("crew") if isinstance(body, dict) else None
    if not isinstance(crew, str) or not crew:
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()
        fork = agent_state.get_fork_info(name)
        if not fork or fork["private_to"] != crew:
            return web.json_response(
                {"error": f"'{name}' is not {crew}'s private copy", "code": "not_a_private_copy"},
                status=409,
            )
        origin = fork["forked_from"]
        try:
            origin_spec, origin_name, _taken, _path = await asyncio.to_thread(
                _load_template_specs, agents_dir, origin, "api_agent_reset"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{origin}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if origin_spec is None:
            # The origin vanished since the fork: rebinding would leave the
            # crew pointing at nothing, so the copy is KEPT and the client is
            # told why — the exact failure the client-side sequence shipped.
            return web.json_response(
                {"error": f"Origin template '{origin}' no longer exists", "code": "origin_missing"},
                status=409,
            )
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        if cfg.agents[crew].kiro_agent != name:
            return web.json_response(
                {"error": f"'{crew}' is no longer bound to '{name}'", "code": "stale_binding"},
                status=409,
            )
        origin_path = _path
        try:
            await asyncio.to_thread(_rebind_crew_locked, crew, (name,), origin_name, origin_path)
        except FileNotFoundError:
            # The origin vanished between validation and the rebind's critical
            # section (a cross-process delete): rebinding would leave the crew
            # pointing at nothing while the copy below gets deleted, so the
            # whole reset refuses instead.
            return web.json_response(
                {"error": f"Origin template '{origin}' no longer exists", "code": "origin_missing"},
                status=409,
            )
        except _StaleBinding:
            return web.json_response(
                {"error": f"'{crew}' is no longer bound to '{name}'", "code": "stale_binding"},
                status=409,
            )
        except _UnverifiableLineage:
            # Ownership of the origin cannot be verified: refuse with the copy
            # kept rather than rebinding onto an unverifiable target.
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{origin_name}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        except Exception:
            logger.exception("reset rebind failed for crew %r", crew)
            return web.json_response(
                {"error": "Could not update the crew's binding", "code": "rebind_failed"},
                status=500,
            )

        # Rebind persisted: the copy is now unreferenced. Delete is
        # best-effort — a failure leaves a hidden private copy, never a
        # broken binding — mirroring _cleanup_superseded's semantics.
        def _delete_copy() -> None:
            # Resolve the copy's ACTUAL file rather than reconstructing it
            # from the declared name: a stem/name divergence (however it
            # arose) would otherwise leave the customized file on disk while
            # its lineage is pruned — a private copy would then appear
            # shared. Prune only after the file is actually gone.
            try:
                _copy_spec, _copy_name, _copy_taken, copy_path = _load_template_specs(
                    agents_dir, name, "api_agent_reset"
                )
            except _AmbiguousTemplateName:
                logger.debug("reset: copy name %r is ambiguous; leaving file and lineage", name)
                return
            if copy_path is None:
                # File already gone — nothing left that could appear shared.
                with contextlib.suppress(Exception):
                    agent_state.prune(name)
                return
            copy_file = copy_path
            if not _spec_path_is_safe(copy_file, agents_dir):
                return
            # This crew was rebound to the source above, so a binding still
            # naming the copy is another crew's: the locked helper keeps file
            # and lineage in that case, atomically against concurrent binding
            # writes. Stem included: it resolves this file.
            if (
                _unlink_copy_unless_referenced(
                    copy_file, agents_dir, name, _copy_name, copy_file.stem
                )
                != "deleted"
            ):
                return
            with contextlib.suppress(Exception):
                agent_state.prune(name)

        await asyncio.to_thread(_delete_copy)
    clear_list_agents_cache()
    state.push_refresh("agents")
    return web.json_response({"ok": True, "template": origin_name})
