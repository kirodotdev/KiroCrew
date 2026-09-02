"""Agent configuration, themes, AIM integration, and agent CRUD handlers."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import agent_state, model_registry
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.acp_backends import selectable_backend_values
from kiro_crew.agent import (
    AGENT_FILENAME,
    _spec_path_is_safe,
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
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.apps.bridges import _mcp_lock as _agent_file_lock
from kiro_crew.apps.bridges import _registration_source
from kiro_crew.apps.manager import (
    INSTALLED_META_FILENAME,
    app_dir,
    app_enabled_state,
    apps_dir,
)
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _safe_color,
    coerce_effort,
    inject_kiro_cli_api_key,
    normalize_agent_model,
    read_config_for_update,
    resolve_agent_bindings,
    resolve_agent_config_path,
    resolve_effective_model,
    write_config_atomically,
)
from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
    _history_key_for,
    is_deprecated_model,
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
    unfixable from the dashboard. Neither rule then acts and the snapshot lands as
    it did pre-fix; enabled apps re-register their servers on the next gateway
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
    """Drop submitted ``<app>:<server>`` entries the installed spec does not hold (#7089).

    THE STALE-SNAPSHOT AXIS, the mirror of ``_merge_unowned_servers``. That rule
    decides what happens to a name the submission OMITS. This one decides one
    narrow question about a name the submission CONTAINS: may the client CREATE a
    name in the app-namespace region THROUGH THIS PUT? Not while the installed spec
    is READABLE -- so a submitted namespaced name with no row on disk is dropped.
    The readability qualifier is not a hedge: an unreadable spec answers nothing, so
    the submission lands as it did pre-fix, and a rule stated without it would
    contradict the best-effort path below.

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
    untouched -- the editor-snapshot-wins contract kept in #5899 and re-affirmed
    for #6664, pinned by ``test_app_owned_entry_present_in_the_snapshot_is_updated``.
    Reversing it needs a maintainer ruling and is tracked separately.

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
        # and the existing verbatim-persist behaviour is unchanged.
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

    MERGE-ON-WRITE (#6664). This PUT persists a whole-file snapshot the client
    read earlier, and ``apps/bridges.py::_register_mcp_servers`` writes app MCP
    bridges into that same file under its own flock. A registration landing
    between the client's read and its PUT was therefore silently clobbered, and
    the app's tools simply stopped resolving with nothing logged anywhere.

    The rule, in one sentence: **preservation requires positive evidence of app
    or host ownership.** An on-disk entry absent from the submission is kept only
    when :func:`_app_or_host_owned` can name its owner by EXACT name; everything
    else is the client's and is deleted, which is what keeps an ordinary entry the
    user typed into this editor deletable -- including one parked under an
    installed app's namespace. If that evidence cannot be read the PUT is refused
    rather than guessed; see :func:`_app_or_host_owned`.

    The inverse test -- "keep anything no mcp.json scope declares" -- reads as
    equivalent and is not. A server added through this same editor lives ONLY in
    the installed spec, which is not a scope, so it looked unowned and was
    re-inserted on every attempt to remove it: not merely preserved against the
    user's wishes, but permanently undeletable, because each retry re-read the
    same entry. Requiring evidence costs an app bridge nothing, since a bridge is
    always positively identifiable.

    The census is NOT consulted, and the reason is worth naming because an
    earlier cut did consult it: it subtracted every scope-declared name from the
    candidates ahead of the ownership test, as precedence carried over from the
    prefix-matching era. With ownership matched by exact manifest name that
    subtraction could only ever remove a name that IS provably owned, so a user
    who also declared ``demo:notes`` in their own mcp.json made every stale PUT
    delete app ``demo``'s live bridge. Proven ownership therefore outranks a
    declaration, and a name with no proven owner is deleted whether a scope
    declares it or not -- which leaves the census unable to change any verdict.

    Two consequences worth naming rather than discovering:

    * A host-MANAGED server the rebuild RE-ADDS (``agent.emission_eligible_mcp_servers``)
      is preserved. That is not a new restriction -- the rebuild re-adds those
      entries unconditionally, so removing one through this editor never stuck.
      The qualifier is load-bearing: a managed entry the rebuild would NOT emit
      (an ``opt_in`` grant, or one whose ``spec_gate`` is shut) is deleted like
      any other absent entry, because nothing re-adds it and preserving it made
      the grant unrevocable and the gated backend resurrectable.
    * An app bridge cannot be removed through this endpoint. That is the issue's
      explicit intent; the app lifecycle (disable/uninstall, which calls
      ``_deregister_mcp_servers``) is what removes it.

    BEST-EFFORT ON AN UNREADABLE SPEC, deliberately -- and the read itself now
    lives in :func:`_on_disk_mcp_servers`, performed once on this rule's behalf and
    on :func:`_drop_unbacked_app_entries`'s, so both directions decide from the
    SAME bytes instead of from two reads that could disagree. A corrupt installed
    spec has no parseable entries to preserve, and this editor is the user's repair
    path for exactly that state -- failing the PUT closed would leave a broken
    agent with no way to fix it from the dashboard. So an unreadable spec preserves
    nothing and the snapshot lands as it did pre-fix; enabled apps re-register
    their servers on the next gateway start
    (``reconcile_enabled_app_resources``), so the loss self-heals.

    WHAT REMAINS, now that the caller holds bridges' flock across this read and
    the spec write (see :func:`_commit_agent_config`). Every writer of this file
    INSIDE the gateway takes that same flock -- ``_register_mcp_servers``,
    ``_deregister_mcp_servers``, ``reregister_app_mcp_servers``, the agent
    rebuild, and ``handlers/mcp.py``'s spec syncs -- so no app registration or
    deregistration can interleave with this read any more, in either direction.
    The earlier writeup here claimed a bounded, self-healing residual for that
    window; that was wrong twice over, and both halves are now moot: the
    deregistration direction was never self-healing (startup reconciliation only
    re-registers ENABLED apps, so a resurrected bridge from a disabled or
    uninstalled app persisted indefinitely), and the window itself is closed
    rather than narrowed.

    The residual that is real is a writer OUTSIDE this process that does not take
    the flock -- kiro-cli writing the spec itself, or a user editing the file by
    hand. Nothing in the gateway can serialize against those, and the same
    exposure applies to every other writer here, so it is a property of the file
    rather than of this change. Torn reads are not part of it: the in-process
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
    # POSITIVE EVIDENCE decides, and nothing overrides it. An earlier cut
    # subtracted every scope-declared name from the candidates BEFORE ownership
    # was tested, as a precedence rule inherited from the prefix-matching era.
    # Once ownership became the EXACT manifest-declared set, that subtraction
    # could only ever remove a name that IS provably owned -- a user who also
    # declares ``demo:notes`` in their own mcp.json made every stale PUT delete
    # app ``demo``'s live bridge. A name with no proven owner is deleted whether
    # a scope declares it or not, so the census cannot change any verdict and is
    # no longer consulted.
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
    host spec not a mapping      n/a (host-produced only)    PRESERVE (pre-fix verdict)      test_a_malformed_host_spec_does_not_fail_the_put
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

    THE HOST ROWS ARE NOT ONE ROW, and collapsing them was a shipped defect. A
    host-managed entry is preserved *because the rebuild re-adds it*, so the
    justification only reaches the entries the rebuild actually emits. It does not
    reach an ``opt_in`` server (``kirocrew-dashboard``: never auto-emitted, and a
    refresh keeps an existing grant current without ever re-granting a removed
    one), which preservation made undeletable through the only surface that can
    revoke the grant; nor a server whose ``spec_gate`` is CLOSED
    (``kirocrew-computer`` on an unsupported platform, or with computer use off),
    which both spec writers ``pop`` — preserving it resurrects exactly the backend
    the gate exists to keep unspawned, and the next rebuild removes it again. The
    eligibility test is therefore the emitter's own
    (``agent.emission_eligible_mcp_servers``), not a second copy here.

    ABSENCE IS PROVEN BY ``lstat`` RAISING ``FileNotFoundError``, nothing weaker.
    ``Path.is_file()`` and ``Path.is_dir()`` answer False for a malformed path as
    readily as for a missing one, so screening on them alone read a broken
    symlink, a directory-where-a-file-belongs, or an unstattable path as "not
    installed" and made that app's live bridges deletable. Every present-but-wrong
    shape raises instead -- see :func:`_require_present_shape`, which screens the
    shape at the CALL SITE so ``manager.app_enabled_state`` keeps the contract its
    other callers rely on. The ENUMERATION obeys the same rule: each child of the
    apps root is stat'ed explicitly rather than filtered through ``is_dir()``,
    because pathlib routes that fault through ``_ignore_error`` and hands back a
    plain False for ENOENT, ENOTDIR, EBADF and ELOOP alike -- so a child that is a
    symlink loop looked like a regular file and was skipped, deleting the bridges
    of the app under that name. Only a resolved stat may exclude a child, and only
    by proving it is not a directory.

    Four justifications carry the rows that are not self-evident:

    * A SCOPE DECLARATION DOES NOT OUTRANK PROVEN OWNERSHIP. An earlier cut
      subtracted every scope-declared name from the candidates ahead of this test,
      as precedence carried over from the prefix-matching era. Against EXACT
      manifest names that subtraction could only ever remove a name that IS
      provably owned: a user who also declares ``demo:notes`` in their own mcp.json
      made every stale PUT delete app ``demo``'s live bridge. A declared name with
      no proven owner is deleted anyway, by the general rule, so the census cannot
      change a verdict and is not consulted.

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
        # ONE MORE SHAPE SCREEN, for the same reason as the two above. The filter
        # here used to be ``p.is_dir()``, which routes its fault through pathlib's
        # ``_ignore_error`` and returns a plain False for ENOENT, ENOTDIR, EBADF
        # and ELOOP -- the same False a regular file gets. A child that is a
        # symlink LOOP was therefore skipped as "not an app" and the absent bridges
        # of the app under that name became deletable. Only a resolved stat may
        # exclude a child, and only by PROVING it is not a directory.
        try:
            st = child.stat()  # follows symlinks, exactly as ``is_dir()`` did
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
    wrong and each one is a defect this span has already shipped. Preserving
    every namespaced entry makes entries permanently undeletable; treating the
    declared set as empty deletes live app bridges over a fault that may be
    transient -- the very clobber #6664 exists to fix. The PUT turns the raise
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

    The lock used to be taken here. It moved out to
    :func:`_commit_agent_config`, which now holds it across the merge's on-disk
    READ as well as this write -- reacquiring it here would deadlock, because
    ``flock`` is per open file description and a second fd on the same file
    blocks against the first from the same thread.

    Still runs in a worker thread, which is what makes the caller's synchronous
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
      is clobbered exactly as it was pre-fix.
    * The governance filter is FIRST among the steps that decide what is
      persisted, and it is in here at all so that the grant decision cannot be
      made against a ceiling that changes before the write publishes it — see
      step (0). Ahead of every write because it persists nothing, so its own
      fail-closed raise costs no partial write.
    * The read is FIRST among the writes' own inputs. It is the only
      fallible-by-decision I/O step, and running it here — immediately adjacent
      to the write it feeds — is what closes the lost-update window: on the
      previous shape the caller read the baseline and the worker wrote it back
      one executor hop later, so a concurrent writer landing in that gap had its
      unrelated fields silently reverted.
    * The bookkeeping lift runs AFTER the ``config.json`` write (so prefix 2
      leaves the sidecar untouched, restoring the pre-lock ordering) but BEFORE
      the spec write, because it STRIPS Kiro Crew keys (``model_managed`` /
      ``cc_model``) out of the same *config* dict the spec write then persists —
      reverse the two and the spec lands with fields kiro-cli's
      ``deny_unknown_fields`` rejects (#2570).

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
    # persists"). It ran in phase 1 until R7: there it decided against a profile
    # snapshot taken BEFORE two lock acquisitions, so a contended transaction
    # flock — unbounded, cross-process — let the ceiling change during the wait
    # and the PUT persisted a grant governance had since withheld. Here no
    # await, no lock release and no other task can land between the decision and
    # the write that publishes it. Same reasoning that put the read at (1).
    #
    # First in the unit, so a raise from the filter (``may_skip_gate_now`` fails
    # closed) leaves all three targets byte-identical, exactly as a phase-1
    # failure did. Its SEL withhold record is infrastructure, not payload, and
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
    # LOCK ORDER IS UNCHANGED: transaction -> config -> bridge-file. The caller
    # already holds the outer two before dispatching this unit, so widening the
    # innermost hold adds no edge and inverts nothing. The cost is that app
    # registration now waits on the ``config.json`` and bookkeeping writes too --
    # the same accepted trade ``remove_provider_entry`` documents for holding the
    # MCP lock across its unlinks, and the alternative (a second, later lock hold
    # for just the spec write) is what reopens the window above.
    #
    # Taken once. ``_write_installed_config`` deliberately no longer locks: with
    # ``flock`` being per open file description, a nested reacquisition from this
    # same thread would block against this hold forever.
    from kiro_crew.platform.governance import sanitize_agent_config_governance

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
    # is clobbered exactly as before the fix. BEFORE the filter, not after, so the
    # entries the merge re-adds are governed too -- re-injecting them afterwards
    # would hand an ``autoApprove`` on a preserved entry a path around step (0).
    #
    # ONE read for the two rules, so they cannot disagree about their baseline:
    # #6664's merge decides names ABSENT from the submission, #7089's rule decides
    # namespaced names PRESENT in a stale one. Their order is immaterial (see
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
    # (1) The one fallible READ, and it precedes every write.
    #
    # Fail closed: writing back a {} baseline would drop every other setting
    # just to record removedTools (see read_config_for_update).
    mc_cfg = read_config_for_update(mc_cfg_path)
    if removed_per_key:
        mc_cfg["removedTools"] = removed_per_key
    else:
        mc_cfg.pop("removedTools", None)
    # (2) config.json — the snapshot the read above produced, still current.
    write_config_atomically(mc_cfg_path, mc_cfg)
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
            #    create/update/delete). This PUT's own RMW now spans an executor
            #    hop, so the event loop no longer serializes it for free: without
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
            # filter has not run yet: it now runs in the commit unit (step 0), so
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
            # NOT in phase 1. The filter used to run here, and that placed the
            # grant decision BEFORE both lock acquisitions: the transaction flock
            # is cross-process and its wait is unbounded, so a ceiling revoked
            # during a contended wait was already stale by the time the write
            # landed, and the PUT restored a grant governance had withheld.
            # ``_commit_agent_config`` step (0) now runs it synchronously
            # adjacent to the writes it governs — see that docstring. It costs a
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
        # capability install/uninstall, the agent-config PUT). The event loop no
        # longer serializes it for free: the PUT's own RMW now runs in a WORKER
        # THREAD, holding this lock across the offload, so an unlocked read here
        # can capture a baseline the worker is about to republish — and the last
        # atomic rename silently reverts the other side's unrelated settings.
        #
        # Loop-side ``async with`` rather than an offload: the lock is an
        # asyncio lock (``LoopBoundLock``) acquired exactly this way by every
        # sibling RMW in this module, and the read and write below are
        # synchronous with NO await between them, so once the lock is held the
        # pair is already indivisible — a worker hop would add a cancellation
        # point where today there is none.
        async with _get_config_lock():
            try:
                data = read_config_for_update(path)
            except ConfigReadError:
                # Fail closed: writing back a {} baseline would drop every other setting.
                logger.exception("Refusing to set default agent: config unreadable")
                return web.json_response(
                    {"error": "failed to read config file", "code": "config_unreadable"},
                    status=500,
                )
            data["default_agent"] = name
            write_config_atomically(path, data)
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
    display, and the #5306 subagent downgrade flag).

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
       onto dotted ``claude-opus-4.8`` (1M, ``opus-4.8-1m``); the old
       ``.``->``-`` fold conflated those two different-window models (#5339).
    3. Fallback for an id the registry does not list (GPT/DeepSeek/Qwen, future
       models, operator-typed ids): the historical lossless fold -- lowercase,
       ``.``->``-`` -- so behavior is identity-preserving off the registered set,
       matching ``from_provider_id``'s pass-through contract.
    """
    string_fold = (name or "").strip().lower().replace(".", "-")
    if not string_fold:
        return ""
    if string_fold in ("default", "auto"):
        return "auto"
    # Registry lookups are exact and its keys/aliases/provider-ids are all
    # lowercase, so resolve on the lowercased id (the old helper lowercased too).
    # canonical_key resolves acp-first then claude_code AND peels a known routing
    # prefix, so it covers both #5339 halves; a miss returns None.
    resolved = model_registry.canonical_key((name or "").strip().lower())
    if resolved is not None:
        return resolved
    return string_fold


def _advertised_cc_models(request: web.Request) -> list[dict]:
    """Map the first active CC provider's advertised models to the API shape.

    claude-agent-acp captures its real versioned list at session init (see
    AcpClient._capture_available_models). Backend provider ids are mapped back to
    canonical registry keys (``from_provider_id``) so they dedup cleanly against
    the registry rows in :func:`_cc_models` and the wire value stays canonical.
    A provider id with no registry entry passes through unchanged (forward-compat
    for models the registry doesn't list yet). Returns ``[]`` when no session has
    initialized or the backend advertised nothing.
    """
    try:
        state: DashboardState = request.app["state"]
        providers = state.sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in providers:
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
                    "model_name": model_registry.from_provider_id(
                        m.get("modelId", ""), "claude_code"
                    ),
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
    same rows whatever the account's tier, so after a downgrade the picker kept
    offering (and kept SHOWING as selected) a model no turn can run. The
    per-session ``session/new`` ``availableModels`` list is the tier-aware one —
    the same signal ``model_is_unusable`` pre-flights against before the wire —
    so when a live session has one, it wins here too. Same rule #1549 applied to
    the claude_code branch in :func:`_cc_models`: advertised is authoritative
    when present.

    The keep/drop decision delegates to ``model_is_unusable`` rather than
    comparing ids here, so the picker cannot disagree with the wire about what
    "this account can run" means. A local comparison would be a second spelling
    of that question — the exact drift that predicate exists to prevent — and any
    difference in how the two fold spelling variants shows up as a row the picker
    offers and the wire then withholds.

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
    kept = [
        m
        for m in models
        if _normalize_model_key(m.get("model_name", "")) == "auto"
        or not model_is_unusable(m.get("model_name", ""), advertised)
    ]
    # Tell "not comparable" apart from "entitled to almost nothing". A backend
    # that advertises `auto` shares a namespace with the catalog by definition, so
    # `auto` alone is a real answer — the most restricted tier there is — and must
    # narrow the picker to it. Only when nothing at all lines up, `auto` included,
    # is this a namespace mismatch (bare vs prefixed provider ids) where showing
    # the whole catalog beats emptying the picker. `auto` is always kept, so it can
    # never serve as the evidence that the two sides are comparable.
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
    used to be offered the full flagship list and only discovered the truth when a
    prompt failed.

    So when anything is advertised, registry rows are FILTERED DOWN to it (keeping
    the registry's cleaner display names for the survivors), and advertised models
    the registry does not list are appended for forward-compat.

    When NOTHING is advertised the registry is shown unfiltered. That is not a
    fallback to the old behaviour by preference -- an empty advertised set means
    "no session has initialized yet", which is indistinguishable from "this account
    gets nothing", and showing an empty picker on a cold dashboard would be worse
    than showing a superset.

    ``auto`` is always present and always FIRST. It is the configured default
    (``config.agent.model``) and a sentinel rather than a real model, so it is
    never filtered by entitlement. It leads the list because the registry's own
    ``default: true`` flag sorts the current flagship to the top, which presented
    a specific paid model as the default in the picker.
    """
    advertised = _advertised_cc_models(request)
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
    seen: set[str] = set()
    for entry in (*registry_rows, *advertised):
        name = entry.get("model_name", "")
        key = _normalize_model_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    # "auto" leads. It may be absent entirely if a future registry drops the row,
    # so synthesize it rather than assuming the filter above preserved one.
    merged = [e for e in merged if _normalize_model_key(e.get("model_name", "")) == "auto"] + [
        e for e in merged if _normalize_model_key(e.get("model_name", "")) != "auto"
    ]
    if not any(_normalize_model_key(e.get("model_name", "")) == "auto" for e in merged):
        merged.insert(0, {"model_name": "auto", "display_name": "Auto", "description": ""})
        seen.add("auto")
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
    """GET /api/models — list available models from the live kiro-cli ACP session."""
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
                        f.unlink()
                    clear_list_agents_cache()
                    # Same reason `declared` goes through `spec_str` above:
                    # `prune` is typed `str` and a structured `name` would reach
                    # it through the `or`.
                    agent_state.prune(declared or name)
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
                                # model means.
                                clear_model_pin(data, agent_name)
                            else:
                                # Explicit pick: freeze it against default bumps.
                                agent_state.set_model_managed(agent_name, False)
                        # Never persist Kiro Crew bookkeeping into the kiro spec —
                        # kiro-cli rejects unknown fields and drops the agent. Same
                        # shared helper as the PUT handler and migrate_agent_specs(),
                        # so this fourth writer can't drift from the other three
                        # (#2570). The model branch above may have just set the
                        # sidecar explicitly; the helper only lifts a stale key out
                        # of `data` when the sidecar is still unset, so it can't
                        # clobber that just-written value. Offloaded like the PUT
                        # handler: the helper does synchronous sidecar read/write
                        # filesystem work that would stall the event loop.
                        await asyncio.to_thread(
                            agent_state.lift_and_strip_bookkeeping, data, agent_name
                        )
                        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
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


async def api_kirocrew_agents(request: web.Request) -> web.Response:
    """GET /api/agents — list all Kiro Crew agent definitions, most-used first.

    Also surfaces the requesting session's project-scope agents
    (``<project>/.kiro/agents``, resolved via ``X-Session-Key``) tagged
    ``scope="project"`` — these dispatch from that slot because kiro-cli runs
    with the slot's project as cwd, so the picker must offer them (#1684's
    headline). A config alias of the same name is listed once, as the alias:
    dispatch resolves aliases first, so the alias is what would answer.
    """
    cfg = KiroCrewConfig.load()
    agents = [
        {"name": name, "scope": "global", **dataclasses.asdict(agent_cfg)}
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
        base = dataclasses.asdict(KiroCrewAgentConfig())
        agents.extend(
            {"name": name, "scope": "project", **base}
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
                key=lambda item: (
                    -usage.get(item[1]["name"], (0, 0.0))[0],
                    -usage.get(item[1]["name"], (0, 0.0))[1],
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
                synced.append(disc.name)

        # Prune agents whose kiro_agent file no longer exists on disk.
        # Only prune package-installed agents (never user-created or kirocrew-owned).
        # Skip pruning if scan returned nothing -- likely a transient issue.
        # Invariant: for package-sourced entries, kiro_agent == dict key == agent name.
        # ("aim" is also accepted for backward-compat with older configs.)
        if discovered_names:
            for name, agent_cfg in list(cfg.agents.items()):
                if agent_cfg.source in ("package", "aim") and (
                    agent_cfg.kiro_agent not in discovered_names
                ):
                    del cfg.agents[name]
                    pruned.append(name)
    except Exception:
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
            cfg.save()
        except Exception:
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
    bindings = resolve_agent_bindings(cfg, agent_name or None)
    # Effort resolves through its own chain, served from the SAME resolver the
    # provider factory calls so the pane cannot disagree with what a session will
    # actually run at -- including the role-aware default, which a crew bound to a
    # background worker agent takes instead of the chat default. Keyed on what the
    # bindings resolved, which is what makes an omitted `agent` answer for the
    # configured default crew rather than for no crew at all.
    crew_effort = cfg.crew_pinned_effort(None, bindings.resolved_alias)
    session_effort = cfg.resolve_session_effort(bindings.kiro_agent, bindings.resolved_alias)
    return web.json_response(
        {
            "model": model,
            "agent": agent_name,
            "kiro_agent": bindings.kiro_agent,
            # Whether the agent itself pins the model, vs inheriting it.
            "pinned": bool(bindings.model),
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
    because the refresh did. A failure costs one gateway lifetime of staleness,
    which is what the behaviour was before this call existed.
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
    # The template pointer must be EXPLICIT. It used to default to "kirocrew",
    # which made every crew created without naming a template an alias for the
    # DEFAULT agent: dispatch flattens an alias to its `kiro_agent`
    # (config.loader.resolve_agent_bindings), so the crew was offered in the chat
    # picker and then the default answered — the "picker reverts to default"
    # report behind #1684. "kirocrew" is still a perfectly valid CHOICE here (a
    # crew booting the built-in agent against its own workspace/memory store is
    # the common case); only the silent default is refused.
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
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' already exists"}, status=409)
        model_reason = _model_pin_rejected(model, request, cfg.agent.provider)
        if model_reason:
            return web.json_response({"error": model_reason, "code": "invalid_model"}, status=400)
        cfg.agents[name] = KiroCrewAgentConfig(
            kiro_agent=kiro_agent,
            workspace=body.get("workspace", "default"),
            memory_store=body.get("memory_store", "default"),
            model=model,
            reasoning_effort=reasoning_effort,
            description=body.get("description", ""),
            triggers=body.get("triggers", ""),
            source=body.get("source", "kirocrew"),
            session_color=session_color,
        )
        cfg.save()
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
    return web.json_response({"ok": True, "name": name})


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
        # Captured BEFORE any mutation: what the effort chain reads today.
        effort_inputs_before = _effort_inputs(agent)
        changed: list[str] = []
        if "kiro_agent" in body:
            agent.kiro_agent = body["kiro_agent"]
            changed.append("kiro_agent")
        if "workspace" in body:
            agent.workspace = body["workspace"]
            changed.append("workspace")
        if "memory_store" in body:
            agent.memory_store = body["memory_store"]
            changed.append("memory_store")
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
        if "source" in body:
            agent.source = body["source"]
            changed.append("source")
        effort_inputs_after = _effort_inputs(agent)
        cfg.save()
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
    return web.json_response({"ok": True, "name": name})


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
        del cfg.agents[name]
        cfg.save()
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
