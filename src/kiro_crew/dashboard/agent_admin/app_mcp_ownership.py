"""Who owns an installed-spec ``mcpServers`` entry: the one on-disk read, the stale-snapshot drop, merge-on-write, and the exact app-declared server census that ``PUT /api/agent/config`` decides against."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        INSTALLED_META_FILENAME,
        _registration_source,
        app_dir,
        app_enabled_state,
        apps_dir,
        emission_eligible_mcp_servers,
        is_link_or_junction,
        loads_user_json,
        logger,
    )


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
        on_disk = loads_user_json(installed_path.read_text(encoding="utf-8"))
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
        #
        # ``lstat`` FIRST, and that ordering is the whole screen rather than a
        # refinement of it. ``stat`` FOLLOWS the link, so a single ``stat`` reports
        # ``FileNotFoundError`` for two opposite states: a name nothing occupies,
        # and a name a DANGLING link or junction still occupies. ``lstat`` inspects
        # the entry itself, so it succeeds for the link and fails only for the empty
        # name -- the rule :func:`_require_present_shape` states for this same
        # hazard one position out ("a dangling symlink counts as present"), applied
        # here, where the entry being screened is the app ROOT.
        try:
            link_st = os.lstat(child)
        except FileNotFoundError:
            # Absence, and only absence, is a skip: an uninstall completing
            # between the listing and this lstat leaves precisely this state, and
            # refusing it would turn a routine PUT into a 500.
            continue
        except OSError as exc:
            raise AppOwnershipUnreadable(
                f"installed-apps entry {child.name!r} present but unstattable: {exc}"
            ) from exc
        try:
            st = child.stat()  # follows symlinks, exactly as ``is_dir()`` does
        except OSError as exc:
            # The name IS occupied -- the ``lstat`` above proved it -- and does not
            # resolve: a dangling link or junction, a symlink loop, or a fault on
            # the target. Whatever this app declares is therefore UNKNOWN, never
            # empty, and empty is what deletes its live bridges. So this is the
            # cannot-read case the metadata screen below already refuses for
            # ``installed.json``, reached one directory level up.
            raise AppOwnershipUnreadable(
                f"installed-apps entry {child.name!r} present but unresolvable: {exc}"
            ) from exc
        if stat.S_ISDIR(st.st_mode):
            entries.append(child)
            continue
        # A non-directory that RESOLVED cleanly splits in two, with opposite answers.
        #
        # LINK-ISH (symlink or junction) whose target is not a directory: the
        # resolving predicates disagree about it, since ``is_dir()`` says no while
        # the name is plainly occupied. So what this app declares is UNKNOWN, and
        # unknown is the one answer that must not be spelled as the empty set,
        # because empty is what deletes its live bridges.
        #
        # A PLAIN non-directory is the opposite answer, and skipping it is
        # deliberate: a file BESIDE the app directories is an ordinary member of a
        # healthy apps root, and refusing it would turn every such file into a 500.
        if stat.S_ISLNK(link_st.st_mode) or is_link_or_junction(child):
            raise AppOwnershipUnreadable(
                f"installed-apps entry {child.name!r} is a link to a non-directory"
            )
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
