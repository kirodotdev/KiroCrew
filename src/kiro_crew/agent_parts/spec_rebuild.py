"""The rebuild, and keeping the installed specs' grants in step with the ceiling.

``rebuild_agent_config`` and the outcome tracking around it; the projection memo a
rebuild records, the hold a conductor spec can be under, the profile watch and the
entry points that re-project the specs when the ceiling or the profile answer moves;
the fork-governance check a session start makes; the reporting wrapper and
``ensure_agent_materialized``; and the installs the rebuild drives itself (AIM
capabilities and packages, the heartbeat agent). New work on the rebuild's order or on
when the specs are re-projected goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`. The state below is this module's initial
binding; ``kiro_crew.agent`` holds the live one, which the functions read and rebind.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from kiro_crew.agent import (
        _HEARTBEAT_AGENT_FILENAME,
        _HEARTBEAT_SYSTEM_PROMPT,
        _MAIN_AGENT_NAME,
        AGENT_FILENAME,
        AmbiguousAgentSpecError,
        ForkGovernanceUnresolved,
        _atomic_json_write,
        _background_agent_model,
        _background_cc_model,
        _claim_propagated_model,
        _decline_shared_agent_home,
        _load_existing_config,
        _read_spec_capped,
        _rebuild_spec_install_admitted,
        _spec_path_is_safe,
        agent_spec_candidates,
        agent_spec_path,
        agent_state,
        agents_spec_lock,
        auto_approve,
        build_agent_config,
        conductor_agents,
        default_spec_commit,
        first_crewmate,
        fork_refresh,
        guide_platform,
        kiro_agents_dir_path,
        logger,
        managed_mcp,
        mcp_aliases,
        mcp_search_path,
        mcp_sources,
        migrate_agent_specs,
        migrate_relocated_skill_uris,
        project_agent_names,
        repair_agent_configs,
        resolved_command_casing,
        service_agents,
        spec_path_key,
        worker_agent,
    )


#: Governance answer generation (ceiling plus profiles) the on-disk ``allowedTools`` was
#: last derived under. Compared for equality only, per ``governance_answer_generation``'s
#: contract.
_projected_ceiling_generation: int | None = None


#: Generation the pending-projection warning last fired for, so a projection a
#: declined instance cannot apply is reported once per ceiling move rather than
#: on every confirming poll.
_pending_projection_warned_generation: int | None = None


#: Whether the LAST ``rebuild_agent_config`` left a conductor spec on disk unwritten
#: (the verdict its ``_held_out`` out-parameter hands the caller that ran it). Kept
#: here for the two readers that did not run the rebuild they have to know about:
#: :func:`prime_ceiling_projection` seeds the ceiling baseline AFTER the boot rebuild,
#: and a baseline seeded over a held spec would mark the current generation
#: projected onto an ``allowedTools`` list that was never re-derived -- with no poll
#: ever retrying it, because the hook skips an unchanged generation; and
#: :func:`retry_held_conductor_specs`, the gateway's hourly maintenance wake, which
#: is the only retry a host WITHOUT central distribution has -- there the hook has no
#: poll to run on at all. Written by every rebuild where its installers' verdicts are
#: settled, so a hold raised by any rebuild that ran before the baseline is seeded
#: reaches it, not only the first, and a retry that rewrites every spec clears it.
_conductor_spec_held: bool = False


#: The conductor installers' own share of ``_conductor_spec_held`` from the last rebuild,
#: before it folds in the crew-bound shared-template hold
#: (``fork_refresh._shared_template_held``). The two are retried differently
#: (:func:`_held_rebuild_would_repeat`) and named apart in the warnings that report a
#: hold (:func:`_hold_cause`).
_conductor_installers_held: bool = False


#: Governance answer generation (ceiling plus profiles) the last
#: ``rebuild_agent_config`` read before deriving any grant, so
#: :func:`prime_ceiling_projection` seeds the baseline with the answer the boot rebuild
#: actually projected rather than whatever a later reader published since.
_rebuild_answer_generation: int | None = None


#: Whether the last ``rebuild_agent_config`` to finish raised rather than completed. A
#: rebuild still running does not set it. :func:`prime_ceiling_projection` then leaves the
#: baseline unseeded, and the profile watch re-projects rather than trusting a memo a
#: half-written rebuild may have overtaken. Set by :func:`_tracks_rebuild_outcome`,
#: cleared by the next rebuild that completes.
_rebuild_incomplete: bool = False


#: Serializes the read-rebuild-record sequence of :func:`reproject_for_ceiling_change`
#: and the profile watch's tick. They run on two threads (the distribution refresher and
#: a worker), and without this a rebuild derived under an older answer can finish last,
#: leave its grants on disk, and still see the memo record the newer answer as projected.
#: Re-entrant because the watch's tick holds it across its call into the hook.
_REPROJECT_LOCK = threading.RLock()


def prime_ceiling_projection() -> None:
    """Record the ceiling generation boot projected the agent config under.

    Called once, before the central-distribution poller starts. Seeding here rather than on
    :func:`reproject_for_ceiling_change`'s first call is the difference between "nothing has
    changed since boot" and "nothing has changed since the first poll" — and the first poll
    can install a new ceiling, so the latter would record that generation and skip the very
    rebuild it needed.

    Seeds nothing while the last rebuild left a conductor spec unwritten
    (``_conductor_spec_held``): that spec's ``allowedTools`` still reflect whatever ceiling
    wrote them, so the current generation is NOT projected, and a seeded baseline would
    have the hook skip every poll until the ceiling moved again -- the one retry lost for
    the process lifetime. An unseeded baseline is the documented safe direction: the first
    poll rebuilds, that rebuild reports its own hold through ``_held_out``, and the memo
    advances only once every spec is rewritten. A host without central distribution has
    no poll; there :func:`retry_held_conductor_specs` is the retry.
    """
    global _projected_ceiling_generation
    from kiro_crew.platform.governance_profiles import governance_answer_generation

    if _conductor_spec_held:
        _projected_ceiling_generation = None
        logger.warning(
            "the boot rebuild left a hold (%s), so the ceiling baseline stays unseeded: "
            "the first distribution poll, or the hourly maintenance wake where no "
            "distribution is configured, retries the rebuild, and on-disk auto-approvals "
            "reflect the previous ceiling until it lands",
            _hold_cause(),
        )
        return
    # The answer the last rebuild derived its grants under, read before it derived any:
    # a profile edit that a later reader published between that rebuild and this seed
    # is then still a move, where reading the store's generation here would absorb it.
    # With no rebuild recorded (it declined before reading), the store is read as it
    # stands, loading it first if nothing has: no grant consulted it, so its first load
    # is not a move. That load walks the directory, so the gateway calls this off-loop.
    if _rebuild_incomplete:
        _projected_ceiling_generation = None
        return
    if _rebuild_answer_generation is not None:
        _projected_ceiling_generation = _rebuild_answer_generation
        return
    from kiro_crew.platform.governance_profiles import (
        poll_profiles_fresh,
        profile_store_loaded,
    )

    if not profile_store_loaded():
        poll_profiles_fresh()
    _projected_ceiling_generation = governance_answer_generation()


def _answer_generation_after_profile_poll() -> int:
    """The governance answer generation, after re-statting the profiles directory.

    ``allowedTools`` and each server's ``autoApprove`` are derived through
    ``may_skip_gate_now``, which consults the ceiling AND every configured profile, so
    the projection memo compares the combined token
    (``governance_profiles.governance_answer_generation``) rather than the ceiling
    counter alone. A profile edit is a file change under the profiles directory and
    reaches that token only when something re-stats the directory, which is what
    ``poll_profiles_fresh`` does here. MAY BLOCK: its callers run on the distribution
    refresher thread or in a worker thread, never on the event loop.
    """
    from kiro_crew.platform.governance_profiles import (
        governance_answer_generation,
        poll_profiles_fresh,
    )

    poll_profiles_fresh()
    return governance_answer_generation()


def _hold_cause() -> str:
    """What the current hold (``_conductor_spec_held``) is on, for the warnings that name it."""
    causes = []
    if _conductor_installers_held:
        causes.append("a conductor spec could not be read or written")
    if fork_refresh._shared_template_held:
        causes.append("a crew-bound shared template could not be read or re-filtered")
    return " and ".join(causes) or "an agent spec could not be read or written"


def _held_rebuild_would_repeat(generation: int) -> bool:
    """Whether a rebuild now could only end held on the same files as the last one.

    True when the hold rests only on crew-bound shared-template files the last pass could
    not read (no conductor installer held, the last rebuild completed), that rebuild
    derived every grant under this same *generation* (``_rebuild_answer_generation``, so a
    move during the rebuild, after the owned specs were written, still rebuilds), and
    ``fork_refresh.shared_template_hold_unchanged`` finds every held file as that pass
    did under *generation*. Such a file may be the one that declares a bound
    name, so the hold stands and the memo stays behind; but the rebuild would rewrite
    ``kirocrew.json`` and the owned specs kiro-cli watches and end held again, so the
    ceiling hook and the maintenance wake skip it. A conductor installer's hold, or a
    rebuild that raised, may clear with nothing on disk changing, so it is retried as
    before. MAY BLOCK: the probe reads each held file once.
    """
    return (
        _conductor_spec_held
        and not _conductor_installers_held
        and not _rebuild_incomplete
        and _rebuild_answer_generation == generation
        and fork_refresh.shared_template_hold_unchanged(generation)
    )


#: Answer generation the gateway's profile watch last attempted a projection for, and
#: when it may attempt that same generation again. A rebuild that is declined or holds a
#: spec logs a refusal and records an audit event per attempt, so the watch retries one
#: on :data:`_PROFILE_WATCH_RETRY_S` rather than on every tick.
_profile_watch_generation: int | None = None


_profile_watch_retry_at: float = 0.0


#: How long the profile watch waits before retrying a projection that did not land.
_PROFILE_WATCH_RETRY_S = 3600.0


#: The profile watch's clock, a module attribute so a test can drive the backoff
#: without touching the process-wide ``time.monotonic`` every other reader shares.
_watch_clock = time.monotonic


#: How long the profile watch waits before retrying a projection whose rebuild raised.
#: Shorter than :data:`_PROFILE_WATCH_RETRY_S` because a raise is often transient (a
#: sharing violation, a lock timeout), and longer than a tick so a persistent one does
#: not rewrite the spec and log a failure every few seconds.
_PROFILE_WATCH_RAISE_RETRY_S = 300.0


def _invalidate_projection_if_answer_moved(answer_generation: int) -> None:
    """Mark the projection memo stale when the answer moved during a rebuild.

    Most rebuilds (a dashboard save, a config change, the maintenance retry) do not run
    under ``_REPROJECT_LOCK``. One that derived its grants under an older answer can
    finish after a projection under the newer answer was recorded, and its write then
    leaves the older grants on disk while the memo says the newer answer is projected.
    Clearing the memo here makes the next distribution poll or profile-watch tick
    re-project. Taken under the lock, so it orders after any projection in flight; the
    lock is re-entrant, so a rebuild the hook itself runs passes straight through.
    """
    global _projected_ceiling_generation
    with _REPROJECT_LOCK:
        if (
            _projected_ceiling_generation is not None
            and _answer_generation_after_profile_poll() != answer_generation
        ):
            _projected_ceiling_generation = None


def reproject_for_profile_change() -> None:
    """The gateway profile watch's tick: re-project when a profile save moved the answer.

    A governance profile is saved by editing a file under the profiles directory. Nothing
    on the distribution poll observes that, and a host with no distribution source has no
    poll, so the gateway calls this on a short interval in a worker thread (it re-stats
    the profiles directory and MAY BLOCK).

    Nothing to do while the projection memo records the current answer, the last
    rebuild completed and no spec is held (``_conductor_spec_held``). A hold can be set
    after the baseline was seeded (the boot path's deferred template pass), and on a
    host with no distribution poll this watch and the hourly maintenance wake are its
    only retries, so a held spec keeps an unchanged answer unprojected here too. A
    rebuild that raised (``_rebuild_incomplete``) may have written
    grants derived under an older answer before it failed, so the memo is cleared and the
    projection re-run. Otherwise it calls :func:`reproject_for_ceiling_change` once for
    the answer, and again only after :data:`_PROFILE_WATCH_RETRY_S` if the projection
    still has not landed (a declined rebuild or a held spec), or after
    :data:`_PROFILE_WATCH_RAISE_RETRY_S` if the rebuild raised.
    """
    global _projected_ceiling_generation, _profile_watch_generation, _profile_watch_retry_at
    with _REPROJECT_LOCK:
        generation = _answer_generation_after_profile_poll()
        if _rebuild_incomplete:
            # The last rebuild raised, possibly after writing grants derived under an
            # older answer, so no recorded projection can be trusted until one completes.
            _projected_ceiling_generation = None
        if generation == _projected_ceiling_generation and not _conductor_spec_held:
            _profile_watch_generation = None
            return
        now = _watch_clock()
        if generation == _profile_watch_generation and now < _profile_watch_retry_at:
            return
        try:
            reproject_for_ceiling_change()
        except Exception:
            _profile_watch_generation = generation
            _profile_watch_retry_at = now + _PROFILE_WATCH_RAISE_RETRY_S
            raise
        if (
            generation == _projected_ceiling_generation
            and not _rebuild_incomplete
            and not _conductor_spec_held
        ):
            _profile_watch_generation = None
            return
        _profile_watch_generation = generation
        _profile_watch_retry_at = now + _PROFILE_WATCH_RETRY_S


def retry_held_conductor_specs() -> bool:
    """Rewrite the conductor specs the last rebuild left unwritten, if there are any.

    The gateway's hourly maintenance wake calls this (``_run_agent_scratch_sweep``),
    and it is the retry a host WITHOUT central distribution otherwise has none of:
    :func:`reproject_for_ceiling_change` runs only as a post-install hook of the
    distribution poller, and ``policy_distribution.start_refresher`` starts no poller
    unless the active policy names a source and a refresh interval. The ceiling itself
    changes on such a host at boot alone (``set_context`` is called by the platform
    bootstrap and by ``apply_ceiling``, nothing watches the policy file), so a
    tightening takes effect at the next boot -- and if THAT boot's rebuild held a
    conductor spec (its read failed past the bounded retry, or its write raised), the
    spec kept the ``allowedTools`` the previous boot's ceiling wrote, kiro-cli kept
    loading them, and nothing in the process was ever going to re-derive them. The
    boot warning and the installer's own name the hold; this closes it.

    The hold is the rebuild's own verdict (``_conductor_spec_held``), so this is a
    no-op on every wake of a healthy host. When a spec IS held, one reporting rebuild
    runs: the memo is rewritten where the four conductor installers' verdicts settle,
    so it clears exactly when every spec was written and stays while any one is not,
    and the next wake retries again. The ceiling baseline is left as it stands: a
    standalone host never reads it, and on a distributed one an unseeded baseline has
    the next confirming poll rebuild once more -- the documented safe direction --
    rather than this path seeding a generation whose ceiling may have moved since the
    rebuild it ran.

    Returns whether every conductor spec is written now.
    """
    if not _conductor_spec_held:
        return True
    from kiro_crew.platform.governance_profiles import governance_answer_generation

    if _held_rebuild_would_repeat(governance_answer_generation()):
        # Only unreadable shared-template files hold, and none has changed since the
        # pass that held on them: a rebuild would end held on them again.
        return False
    logger.info("retrying the conductor specs the last agent-config rebuild left unwritten")
    held_out: list[bool] = []
    _path, wrote = rebuild_agent_config_reporting(_held_out=held_out)
    if wrote and not (held_out and held_out[0]):
        logger.info(
            "the conductor specs an earlier rebuild left unwritten are rewritten under "
            "the current governance ceiling"
        )
        return True
    logger.warning(
        "an agent spec is still unwritten after the retry (%s); its on-disk "
        "auto-approvals reflect the previous ceiling until the next maintenance wake "
        "rewrites it -- repair it, or run `kirocrew setup --agent-only --clean` to "
        "rebuild a conductor spec",
        _hold_cause(),
    )
    return False


def reproject_for_ceiling_change() -> None:
    """Re-derive the on-disk ``allowedTools`` when the governance ceiling has moved.

    ``allowedTools`` is kiro-cli's blanket auto-approve list, and it is **materialised**: the
    five writers of it consult the ceiling when they write, and kiro-cli then reads the FILE.
    So a ceiling that comes to deny a tool mid-flight does not narrow a list already on disk,
    and every session started afterwards would keep auto-approving what the fleet now
    forbids — the tool short-circuits inside the harness and never reaches Kiro Crew's own
    PreToolUse gate.

    Registered as a post-install hook on the central-distribution refresher, alongside the
    tailnet revocation and for the same reason: before a live refresh existed the ceiling
    only changed at boot, and boot projects the config anyway.

    **Serialized.** It runs on the refresher thread and from the gateway's profile watch,
    so the whole read-rebuild-record sequence holds ``_REPROJECT_LOCK``: otherwise a
    rebuild derived under an older answer could finish last and leave its grants on disk
    while the memo records the newer answer as projected.

    **A profile save counts as a move.** The writers consult the ceiling AND every
    configured governance profile (``may_skip_gate_now``), so a saved profile that comes to
    govern an auto-approved tool needs the same re-derivation. The memo therefore compares
    ``governance_answer_generation()`` (ceiling plus profiles), read after re-statting the
    profiles directory, and the gateway's periodic profile watch
    (:func:`reproject_for_profile_change`) calls this too, whether or not a distribution
    source is configured.

    **Bounded to an actual change.** Hooks run on every confirming poll, so an unconditional
    rebuild would rewrite a file kiro-cli watches every refresh interval, for nothing. The
    baseline is seeded by :func:`prime_ceiling_projection` BEFORE the poller starts, not on
    the first call: the first poll can itself install a new ceiling, and a first-call baseline
    would record that generation and skip the very rebuild it needed.

    **The memo advances only after a successful rebuild.** A failure raises through the hook
    runner, which logs it and moves on — and if the generation had already been marked
    synchronised, the retry the next poll would otherwise give us is lost, leaving forbidden
    auto-approvals on disk for the process lifetime.

    An unseeded baseline rebuilds once on the first call rather than skipping, which is the
    safe direction: a redundant rewrite costs a file write, a skipped one costs the tighten.

    What this cannot do is narrow a session ALREADY negotiated: kiro-cli holds the grants it
    was given, and no policy mechanism reaches into a running one. That limit is the same
    shape as an already-running process keeping its own sandbox, and a restart is its only
    answer — which is why removing live refresh would not close it either.
    """
    with _REPROJECT_LOCK:
        _reproject_holding_the_lock()


def _reproject_holding_the_lock() -> None:
    """The body of :func:`reproject_for_ceiling_change`, run under ``_REPROJECT_LOCK``."""
    global _projected_ceiling_generation
    # The combined ceiling + profile token, read after re-statting the profiles directory.
    governance_generation = _answer_generation_after_profile_poll

    generation = governance_generation()
    if _projected_ceiling_generation == generation and not _conductor_spec_held:
        # A hold set after the baseline was seeded (the boot path's deferred
        # template pass) keeps an unchanged generation unprojected.
        return
    if _held_rebuild_would_repeat(generation):
        # Held only on shared-template files that still cannot be read and have not
        # changed under this answer: the memo stays behind, no rebuild runs.
        return
    held_out: list[bool] = []
    _path, wrote = rebuild_agent_config_reporting(_held_out=held_out)
    if not wrote or bool(held_out and held_out[0]):
        # Either the shared specs are not this instance's to rewrite, or a
        # conductor spec on disk was left unwritten -- it could not be read, or
        # its installer raised on the write (``conductor_agents._governed_grants``,
        # ``rebuild_agent_config``) -- in both cases the moved
        # ceiling was NOT projected onto every ``allowedTools`` list on disk,
        # and the memo must say so. The refusal verdict comes from the SAME
        # evaluation that gated the write, inside the rebuild itself: probing
        # the guard first and then rebuilding leaves a window where a
        # concurrent default-home boot rewrites the specs between the two
        # reads, the rebuild refuses, and a memo advanced on the stale probe
        # marks the generation synchronised while the on-disk ``allowedTools``
        # were never narrowed -- silently auto-approving tools the ceiling now
        # forbids (the harness short-circuits them before PreToolUse). Leaving
        # the memo behind keeps the advance-only-after-success rule literal
        # for refusals: every later poll retries, and the pending generation
        # is projected the moment the refusal clears instead of being lost
        # for the process lifetime.
        #
        # The pending state is the security-relevant half of the refusal, so
        # it logs at WARNING -- the same tier as the decline itself -- but once
        # per generation rather than per confirming poll, which fires every
        # refresh interval. The decline arm's own refusal warning and SEL
        # event, and the installer's warning naming the spec it left alone,
        # fire per attempt, which is the audit posture every other caller of
        # the rebuild already has.
        global _pending_projection_warned_generation
        if _pending_projection_warned_generation != generation:
            _pending_projection_warned_generation = generation
            logger.warning(
                "ceiling generation %s is pending: this instance may not rewrite "
                "the shared agent home, or an agent spec on disk (a conductor spec or "
                "a crew-bound shared template) could not be read or written and was "
                "left unchanged, so on-disk auto-approvals "
                "still reflect the previous ceiling until the owning instance "
                "projects the new one (or this instance's refusal clears)",
                generation,
            )
        return
    logger.info(
        "the governance ceiling or a governance profile moved; re-derived the agent "
        "config's auto-approvals"
    )
    _projected_ceiling_generation = generation


def _tracks_rebuild_outcome(fn: Any) -> Any:
    """Mark ``_rebuild_incomplete`` and clear the projection memo when the rebuild raises.

    A decorator rather than a ``try`` in the rebuild itself, so the whole body (several
    hundred lines with exits of their own) stays as it is. Completion is recorded inside
    the body, where the answer it derived under is in hand.
    """

    @functools.wraps(fn)
    def tracked(*args: Any, **kwargs: Any) -> Any:
        global _projected_ceiling_generation, _rebuild_incomplete
        try:
            return fn(*args, **kwargs)
        except BaseException:
            # The rebuild may have written grants derived under an older answer before it
            # raised, so the recorded projection is cleared here, under the lock, rather
            # than left to a flag a later rebuild's completion would reset: the next poll
            # or watch tick then re-projects over whatever this one wrote.
            with _REPROJECT_LOCK:
                _rebuild_incomplete = True
                _projected_ceiling_generation = None
            raise

    return tracked


@_tracks_rebuild_outcome
def rebuild_agent_config(
    *,
    clean: bool = False,
    refresh_forks: bool | Literal["defer"] = True,
    _wrote_out: list[bool] | None = None,
    _held_out: list[bool] | None = None,
) -> Path:
    """Rebuild and write the merged kirocrew.json to ~/.kiro/agents/.

    This is the single authoritative function for producing the agent config.
    It reads all source files, merges with correct priority, resolves commands,
    and injects fresh AIM skill paths.

    Merge priority (highest wins):
      1. ~/.kiro/crew/mcp.json (agent-specific overrides)
      2. ~/.kiro/settings/mcp.json (kiro global, fills gaps)
      3. Existing kirocrew.json (preserves user customizations)
      4. Bundled defaults (security, managed servers)

    --skill-paths are always resolved fresh from AIM manifests regardless
    of what any source file contains.

    When the config already exists and *clean* is False, the existing file
    is used as the base so that **all** user customizations are preserved.
    Only security-critical ``hooks`` and dynamic fields (``prompt`` URI,
    kirocrew MCP server commands) are refreshed from defaults.

    Returns the spec path whether it wrote or refused. A caller that must
    tell the two apart (:func:`reproject_for_ceiling_change`, whose memo may
    only advance on a confirmed write) uses
    :func:`rebuild_agent_config_reporting`, which reads the verdict through
    the private *_wrote_out* out-parameter — the verdict comes from the SAME
    single evaluation of :func:`_decline_shared_agent_home` that gates the
    write below, never from a separate probe a concurrent default-home boot
    could race. The conductor installers' own reports of whether they wrote
    travel separately, through *_held_out*, because the two verdicts have
    different readers: a refusal means ``kirocrew.json`` was NOT rewritten
    (the dashboard's default-model applier reads that as "the change did not
    land"), while a conductor spec held on disk leaves ``kirocrew.json``
    written and only the ceiling memo with something left to do.

    Args:
        clean: If True, ignore existing config and regenerate from defaults.
        _wrote_out: private — when given, receives one bool: ``True`` after
            the write landed and the whole function returned, ``False`` when
            the shared-home guard refused. Pass a FRESH empty list: the
            reader consumes the first element, so a reused list misreports.
        _held_out: private — when given, receives one bool: ``True`` when a
            conductor installer left its spec on disk unwritten — because the
            file could not be read for a reason that may clear on retry
            (``conductor_agents._governed_grants``), or because the installer
            raised and the failure was logged rather than propagated — so an
            ``allowedTools`` list on disk was NOT re-derived and a moved
            ceiling must stay pending rather than be marked projected.
            ``False`` otherwise, including when the guard refused (nothing
            ran). Same fresh-list rule as *_wrote_out*.
    """
    declined = _decline_shared_agent_home()
    if declined is not None:
        if _wrote_out is not None:
            _wrote_out.append(False)
        if _held_out is not None:
            _held_out.append(False)
        return declined

    kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    # The governance answer this rebuild derives its grants under, read before any
    # grant is derived, so a profile edit that lands mid-rebuild still reads as a move.
    # Published only once the rebuild completes (below); a rebuild that raises is marked
    # by ``_tracks_rebuild_outcome`` instead, so a seed taken over it projects nothing.
    global _rebuild_answer_generation, _rebuild_incomplete
    answer_generation = _answer_generation_after_profile_poll()
    path = kiro_agents_dir_path() / AGENT_FILENAME

    # One-time (idempotent) self-heal: strip Kiro Crew bookkeeping keys from
    # every kiro agent spec into the sidecar so kiro-cli accepts them all.
    migrate_agent_specs()
    # A relocated builtin skill keeps working for agents that map its old path.
    migrate_relocated_skill_uris()

    # Managed MCP sync happens after config is fully built (see below).

    # One spec-gate snapshot for the whole rebuild, so the emit path below and the
    # withhold audit near the end describe the SAME decision (see
    # _gated_off_servers).
    gated_off = managed_mcp._gated_off_servers()

    # App MCP-key ownership as it stands BEFORE any of this rebuild's work, so a
    # key whose app is uninstalled while the rebuild runs is still known to have
    # been app-owned. The reconcile at the end reads ownership again and requires
    # a POSITIVE enablement answer for any key either read claims: an app that
    # vanished answers nothing, and defaulting that to "exempt" would leave its
    # auto-approve grant on the name for whatever is bound there next.
    _app_owned_at_start = mcp_sources._app_owned_mcp_keys()

    if not clean and path.exists():
        # Existing config — preserve user customizations, only refresh
        # security-critical and dynamic fields.
        config, fresh_install = _load_existing_config(path, gated_off=gated_off)
    else:
        config = build_agent_config(gated_off=gated_off)
        fresh_install = True

    # Seed default-model tracking for a fresh/clean build. A clean regen always
    # resumes tracking the shipped default; a first-time install seeds tracking
    # only when the sidecar has no prior (possibly frozen) choice to preserve.
    #
    # That prior choice is why the first-time seed is one strict, locked
    # read-and-write (``claim_model_managed_if_unset``) rather than a lenient
    # read followed by a write: a lenient read answers ``None`` for an
    # unreadable sidecar too, and seeding on it would write "track the shipped
    # default" over a pin the owner recorded, a value kept nowhere else. An
    # unreadable sidecar raises here, before the spec is written, so the next
    # rebuild is still a fresh install and seeds then; a sidecar that stays
    # unreadable fails this rebuild the same way every sidecar write does.
    main_name = config.get("name") or _MAIN_AGENT_NAME
    if fresh_install:
        if clean:
            agent_state.set_model_managed(main_name, True)
        else:
            agent_state.claim_model_managed_if_unset(main_name)

    # Merge shared MCP servers from ~/.kiro/settings/mcp.json (Kiro user-level
    # config) FIRST.  Kiro Crew is kiro-first (ACP/kiro-cli only), so Kiro
    # global OUTRANKS the Claude Code global on collisions — setdefault makes
    # the first writer win.  Skip managed servers — their command/args are set
    # by _refresh_dynamic_fields() and must not be overwritten by stale global
    # entries.  Write-through is never done here (Kiro Crew reads globals but
    # never mutates them).
    #
    # NOTE: this reverses the prior "CC global wins over Kiro global" rule.
    # See docs/architecture/mcp.md. CC global is kept only as a gap-filler so
    # the Claude Code provider can be re-enabled later without rework; it must
    # not shadow a Kiro-global entry.
    sources = mcp_sources.merge_mcp_sources(config)

    # Resolve MCP commands to absolute paths and validate.
    #
    # Resolution-aware fallback: a server can be defined in several sources
    # with different commands.  If the merged winner's command does not
    # resolve (e.g. a bare command whose binary isn't on the rebuild PATH —
    # the classic internal-MCP-server shadowing case), fall back to the SAME server's
    # spec from the other sources before dropping it, in priority order
    # (kirocrew > kiro-global > provider-global).  This prevents one source's
    # unresolvable command from killing a server another source can resolve.
    def _resolve_command(cmd: str, env: dict | None) -> tuple[str | None, str]:
        """Resolve an MCP command to an absolute path, plus the path searched.

        Returns ``(resolved_or_None, search_path)``. The second element is what
        lets the drop warning name the directories actually consulted; it is ""
        when no PATH search happened (empty command, or an absolute command
        accepted directly).

        Accepts an absolute path directly when the file exists and is
        executable — shutil.which can fail inside user-namespace sandboxes
        even when the file is fine.

        Searches the server's own env.PATH first, then the contributed MCP
        directories, then the same augmented PATH the MCP probe uses — all via
        :func:`mcp_search_path`, so resolution, the probe and the rewriter all
        agree. A divergence would let a server probe healthy
        on the dashboard while being silently dropped from the generated agent
        config ("command not found: kirocrew"). The value EMITTED into the spec
        is :func:`spec_env_path` instead, which omits the contributed
        directories: an emitted PATH is persisted and read back as an authored
        entry, so a contributed directory written there could never be removed
        again. augmented_path
        covers AIM's mcp-servers directory and ~/.toolbox/bin and appends the running
        interpreter's console-scripts dir
        (venv ``Scripts\\`` on Windows, ``bin/`` on POSIX) as a last-resort
        fallback for pip-generated wrappers like ``kirocrew``.
        """
        if not cmd:
            return None, ""
        if os.path.isabs(cmd) and os.path.isfile(cmd) and os.access(cmd, os.X_OK):
            return cmd, ""
        # Case-insensitive PATH key: a Windows-authored spec says "Path", and
        # resolving against a DIFFERENT path than the emitted spec carries would
        # reopen the probe/session split from the other side.
        _env = env or {}
        _key = spec_path_key(_env)
        _declared = _env.get(_key, "") if _key else ""
        _search = mcp_search_path(_declared if isinstance(_declared, str) else "")
        # A command carrying a directory component is not PATH-searched:
        # ``shutil.which`` returns before it reads ``path=`` when
        # ``os.path.dirname(cmd)`` is truthy, checking exactly the one location
        # the command names. Reporting ``_search`` for it would send the reader
        # to audit directories that were never consulted, which is the opposite
        # of the not-installed/installed-elsewhere distinction this path draws --
        # so return "" as the searched path even though the lookup still runs.
        #
        # Both lookups pass through ``resolved_command_casing``: the value
        # returned here is PERSISTED as the spec's absolute ``command``, and an
        # absolute path is accepted verbatim on the next pass, so a PATHEXT-
        # synthesized ``.EXE`` written once would be indistinguishable from an
        # operator's own spelling from then on. Repairing at the resolver, not
        # at the persist site, also keeps the provenance record's ``emitted``
        # value repaired, so ``command_is_ours`` still recognises the entry.
        if os.path.dirname(cmd):
            return resolved_command_casing(shutil.which(cmd, path=_search)) or None, ""
        # The search path is returned, not recomputed by the caller: a candidate
        # that declares its own ``env.PATH`` is searched against a DIFFERENT path
        # than one that does not, so a caller reporting ``mcp_search_path("")``
        # would name directories that were never searched.
        return resolved_command_casing(shutil.which(cmd, path=_search)) or None, _search

    resolved = mcp_sources.resolve_mcp_servers(config, sources, _resolve_command)
    mounted = mcp_aliases.normalize_server_keys(config, resolved.unresolved)

    # Sync shared (user-installed) servers to tools/allowedTools.
    # These are explicitly installed by the user via AIM's installer or
    # manual mcp.json edits — unlike managed servers, they should always
    # be registered regardless of fresh/existing config state.
    mcp_sources.sync_shared_server_refs(config, sources, mounted)
    managed_mcp.register_managed_refs(config, fresh_install=fresh_install, gated_off=gated_off)
    guide_grant_pending = guide_platform.grant_guide_platform_once(
        config, fresh_install=fresh_install
    )
    mate_grant_pending = guide_platform.grant_mate_platform_once(
        config, fresh_install=fresh_install
    )

    # Final dedup (preserves order).
    for key in ("tools", "allowedTools"):
        config[key] = list(dict.fromkeys(config.get(key, [])))
    auto_approve.final_ceiling_pass(config)

    # The provenance-bearing write happens under the agent-spec lock, and the
    # guard is asked AGAIN once the lock is held. A default-home and an
    # override-home gateway booting together can both pass the check at the top
    # (both read "no spec"); serialized here, the second one reads the spec the
    # first one just wrote and refuses instead of landing last. The lock narrows
    # that interleave and is not the gate: when it cannot be taken (an on-loop
    # caller under contention) the re-check still runs and the write proceeds
    # unserialized, because refusing would leave a fresh install with no spec.
    with contextlib.ExitStack() as spec_lock:
        try:
            spec_lock.enter_context(agents_spec_lock(kiro_agents_dir_path()))
        except OSError:
            logger.debug("default spec written without the agent-spec lock", exc_info=True)
        if _decline_shared_agent_home(audit=False) is not None:
            declined = _decline_shared_agent_home()
            if _wrote_out is not None:
                _wrote_out.append(False)
            if _held_out is not None:
                _held_out.append(False)
            return declined if declined is not None else path
        default_spec_commit.write_default_spec(
            path,
            config,
            clean=clean,
            gated_off=gated_off,
            sources=sources,
            resolved=resolved,
            app_owned_at_start=_app_owned_at_start,
        )
    logger.info("Installed agent config: %s", path)
    _claim_propagated_model(config, main_name)
    if guide_grant_pending:
        guide_platform.mark_guide_platform_granted()
    if mate_grant_pending:
        guide_platform.mark_mate_platform_granted()

    # The guard ADMITTED this rebuild above, and that one decision now covers
    # every derived spec it installs. Re-asking per write would answer a
    # different question: ``write_default_spec`` has just created
    # ``kirocrew.json``, and the temp-checkout arm declines precisely when a
    # spec is present -- so each spec below would be refused against the file
    # this rebuild wrote. See ``_rebuild_spec_install_admitted``.
    #
    # ``finally``, not a plain reset after the block: several of these calls can
    # raise past their own arms, and a leaked flag would exempt every later
    # write in the process -- a worker dispatch, a dashboard edit -- from the
    # spec-install guard.
    global _conductor_spec_held, _conductor_installers_held
    _rebuild_admitted_token = _rebuild_spec_install_admitted.set(True)
    try:
        # Install Kiro Crew AIM capabilities package (includes kirocrew-lite)
        _install_aim_capabilities()

        # Install kirocrew-knowledge agent (used by Knowledge Library LLMPool)
        try:
            service_agents._install_knowledge_agent()
        except Exception:
            logger.debug("kirocrew-knowledge agent install failed", exc_info=True)

        # Tracks a failed research rewrite the same way ``team_lead_held`` below tracks
        # team-lead. ``_install_research_agent`` derives from ``build_agent_config()``,
        # so its ``allowedTools`` is re-filtered through the governance ceiling on every
        # boot -- a ceiling-TIGHTENING rebuild whose research rewrite failed leaves the
        # previous list, with the now-revoked grants, on disk. Swallowing that at debug
        # would let ``reproject_for_ceiling_change`` advance its generation memo so the
        # next poll would NOT retry, and the stale grants would persist for the process
        # lifetime. Mark the hold instead, exactly like team-lead and the conductor specs
        # (a ceiling-filtered installer must not be recorded as synced over an unrewritten
        # grant list). Folded into ``conductor_held`` beside ``team_lead_held`` below.
        research_held = False

        # Install kirocrew-research agent (used by the Research Lab campaign loop)
        try:
            service_agents._install_research_agent()
        except Exception:
            research_held = True
            logger.warning(
                "kirocrew-research agent install failed; marking the ceiling held so a "
                "tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Tracks a failed mate-conductor rewrite the way ``research_held`` and
        # ``team_lead_held`` do: ``_install_mate_conductor_agent`` also derives
        # from ``build_agent_config()``, so a failed rewrite under a tightening
        # ceiling must hold the memo rather than let it advance over a stale
        # grant list on disk. Folded into ``conductor_held`` below.
        mate_conductor_held = False

        # Install kirocrew-mate-conductor agent (the agent a crewmate's own chat
        # runs on) and kirocrew-mate-worker (what its worker sessions run on).
        try:
            service_agents._install_mate_conductor_agent()
            service_agents._install_mate_worker_agent()
        except Exception:
            mate_conductor_held = True
            logger.warning(
                "kirocrew-mate-conductor agent install failed; marking the ceiling held so "
                "a tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Tracks a failed team-lead rewrite the way ``conductor_held`` tracks the
        # conductor specs. It is folded into ``_conductor_spec_held`` AFTER that flag
        # is settled below, because the settle is a plain assignment that would
        # otherwise clobber a hold set here (this installer runs before it).
        team_lead_held = False

        # Install kirocrew-team-lead agent (a default agent an operator selects like
        # any other). Its ``allowedTools`` is re-filtered through the governance
        # ceiling on every boot, exactly like the conductor specs above -- it derives
        # from ``build_agent_config()`` and passes its added dispatch grants through
        # the same ceiling filter before the write.
        # So a ceiling-TIGHTENING rebuild whose team-lead rewrite failed (a read-only
        # file, a refused ``os.replace``, a spec-lock timeout) leaves the previous
        # list -- with the now-revoked grants -- on disk. Swallowing that at debug
        # would let ``reproject_for_ceiling_change`` advance its generation memo and
        # the next poll would NOT retry, so the stale grants would persist for the
        # process lifetime. Mark the hold instead, like the conductor
        # specs and the dashboard-author below: ``prime_ceiling_projection`` then does
        # not seed the ceiling as projected, and ``retry_held_conductor_specs`` retries
        # the rewrite on the next maintenance poll. With the spec simply absent the
        # lead is not offered and nothing else depends on it; the hold is only about
        # not recording a tightened ceiling as synced over an unrewritten grant list.
        try:
            service_agents._install_team_lead_agent()
        except Exception:
            team_lead_held = True
            logger.warning(
                "kirocrew-team-lead agent install failed; marking the ceiling held so a "
                "tightened ceiling is retried rather than recorded as synced over the "
                "stale grant list left on disk",
                exc_info=True,
            )

        # Install kirocrew-dashboard-manager agent (the subagent a crewmate hands page
        # work to). Degrades to a debug line like the two above: with the spec absent a
        # crewmate still has `dashboard_fields` and `dashboard_write`, so the page it has
        # keeps working and only CHANGING the page is unavailable.
        try:
            service_agents._install_dashboard_manager_agent()
        except Exception:
            logger.debug("kirocrew-dashboard-manager agent install failed", exc_info=True)
        # Every install, fresh or upgraded, creates its first crewmate once (a
        # one-time marker, so a deleted one is never re-created). The reserved
        # ``default`` member is never touched. Boot keeps going either way.
        try:
            first_crewmate.create_first_crewmate_once()
        except Exception:
            logger.warning("First crewmate creation failed", exc_info=True)

        # Install kirocrew-heartbeat agent (used by HeartbeatService for unattended polling)
        try:
            _install_heartbeat_agent()
        except Exception:
            logger.debug("kirocrew-heartbeat agent install failed", exc_info=True)

        # Install kirocrew-conductor agent (goal decomposition + session-control dispatch).
        # ``clean`` is passed through: the conductor installers carry the user's own
        # ``allowedTools`` entries forward across a rebuild, and a clean rebuild is the
        # explicit reset that drops them, as it drops every customization above. Each
        # reports whether it wrote: a spec left on disk because it could not be read
        # is a list this rebuild did NOT re-derive, and so is one whose installer
        # raised -- a failed write (a read-only file, a refused ``os.replace``) leaves
        # the previous list on disk exactly as a refused read does -- and ``_held_out``
        # must say so in both cases, or a moved ceiling is marked projected onto a
        # list that still carries the old grants.
        conductor_held = False
        try:
            conductor_held |= not conductor_agents._install_conductor_agent(clean=clean)
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-conductor agent install failed", exc_info=True)

        # Install kirocrew-pipeline-conductor agent (repository pipeline fleet supervision)
        try:
            conductor_held |= not conductor_agents._install_pipeline_conductor_agent(clean=clean)
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-pipeline-conductor agent install failed", exc_info=True)

        # Install the deprecated kirocrew-ledger-conductor alias (the same spec as
        # kirocrew-conductor above, under its old name, for one release). EAGER for
        # the same forced reason spelled out on the worker below: ``session_create``
        # refuses an agent it cannot resolve, and resolution reads a boot-time
        # in-memory snapshot that no spec write refreshes — so a lazily-materialized
        # spec is invisible to the validation that runs ahead of the spawn. A session
        # already running under the old name resolves it on every dispatch, which is
        # what the alias exists to keep working.
        try:
            conductor_held |= not conductor_agents._install_ledger_conductor_agent(clean=clean)
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-ledger-conductor alias install failed", exc_info=True)

        # Install kirocrew-security-conductor agent (one security audit's worker fleet)
        try:
            conductor_held |= not conductor_agents._install_security_conductor_agent(clean=clean)
        except Exception:
            conductor_held = True
            logger.debug("kirocrew-security-conductor agent install failed", exc_info=True)
        # Settled here, not at the end: ``prime_ceiling_projection`` seeds the ceiling
        # baseline from this after the boot rebuild, and a rebuild that raises further
        # down must still leave the installers' verdict behind, not the previous one.
        # ``team_lead_held`` folds in here (not only into ``_conductor_spec_held``)
        # because the end-of-rebuild ``_held_out.append`` reads ``conductor_held``.
        # ``research_held`` and ``mate_conductor_held`` fold in for the same reason:
        # all three derive from ``build_agent_config`` and must not be recorded
        # synced over a stale grant list.
        conductor_held = conductor_held or team_lead_held or research_held or mate_conductor_held
        _conductor_spec_held = conductor_held
        _conductor_installers_held = conductor_held

        # Install kirocrew-dashboard-author agent (authors one dashboard template and lands
        # it as a PR). EAGER for the same forced reason spelled out on the worker above:
        # ``session_create`` refuses an agent it cannot resolve, resolution reads a boot-time
        # in-memory snapshot that no spec write refreshes, so a lazily-materialized spec is
        # invisible to the validation that runs ahead of the spawn -- a conductor could never
        # dispatch it on a clean install. Being here also re-filters its grants through the
        # governance ceiling on every boot, so the spec normally cannot outlive a tightened
        # ceiling -- with one exception: a file at the stem that is NOT this installer's own
        # (its bytes do not reproduce the installer-recorded ownership digest) is left untouched,
        # so a hand-authored user spec at the once-user-creatable stem -- or a copy of another
        # owned agent renamed onto it -- is never overwritten. Ownership is the digest the
        # installer records in the ``agent_state`` sidecar for this name.
        dashboard_author_install_error: Exception | None = None
        try:
            worker_agent._install_dashboard_author_agent()
        except Exception as exc:
            # Capture, do NOT swallow. An installer failure must not ABORT the independent
            # repairs below (fork governance refresh, hook repair) -- those run for every other
            # installer's failure too -- so we let them run first. But it must not be reported as
            # a SUCCESSFUL rebuild either: this spec's ``allowedTools`` is re-filtered through the
            # governance ceiling on every rebuild, so a ceiling-TIGHTENING rebuild whose
            # author-spec rewrite failed (e.g. a Windows sharing violation, a spec-lock timeout)
            # leaves the spec's forbidden auto-approvals live. If this rebuild then reported
            # success, ``reproject_for_ceiling_change`` would advance its generation memo and the
            # next poll would NOT retry -- the forbidden grants would persist for the process
            # lifetime. So the failure is re-raised at the END of the rebuild (after the
            # independent repairs), which (a) escapes before ``_wrote_out`` is marked True, so
            # ``rebuild_agent_config_reporting`` does not report a write, and (b) leaves the
            # reprojection memo behind so the next poll retries -- the safe direction the
            # success contract already documents for a post-write exception.
            dashboard_author_install_error = exc
            # A tightened ceiling whose author-spec rewrite failed leaves revoked auto-
            # approvals live. Mark the hold so ``prime_ceiling_projection`` does not seed
            # the ceiling as projected, and ``retry_held_conductor_specs`` retries the
            # rewrite on the next maintenance poll; without the hold a boot-time author
            # install failure loses the governance retry.
            _conductor_spec_held = True
            _conductor_installers_held = True
            logger.warning(
                "kirocrew-dashboard-author agent install failed; completing independent repairs "
                "then failing the rebuild so a tightened ceiling is retried, not marked synced",
                exc_info=True,
            )

        # Install kirocrew-worker agent (the default toolset plus the work-ledger set).
        #
        # EAGER, like its six siblings above, and that placement is forced rather than
        # chosen. ``session_create`` refuses an agent it cannot resolve
        # (``agent_unresolved``), resolution runs through
        # ``config.loader._materialized_kiro_agent``, and that is a pure IN-MEMORY
        # snapshot refreshed at boot and by app (de)registration — never by a spec
        # write. A spec materialized on the spawn path is therefore invisible to the
        # validation that runs ahead of the spawn, so on a clean install a conductor
        # cannot dispatch a worker at all. Measured: the name resolves False in the boot
        # snapshot, and still False after a lazy write until a refresh nothing triggers.
        #
        # Being here also means every boot re-filters this spec's grants through the
        # governance ceiling, exactly as it does for the six siblings, so the spec
        # cannot outlive a tightened ceiling.
        try:
            worker_agent._install_worker_agent()
        except Exception:
            logger.debug("kirocrew-worker agent install failed", exc_info=True)

        # Bidirectional sync: ensure packages installed for one provider
        # are also available for the other (agents↔plugins, skills).
        sync_aim_packages()

        fork_refresh.refresh_after_rebuild(refresh_forks, gated_off)
        if refresh_forks is True and fork_refresh._shared_template_held:
            # A crew-bound shared template kept grants the ceiling may now deny
            # because its rewrite failed. Held like a conductor spec, so the
            # ceiling memo stays behind and the next poll or wake retries.
            conductor_held = True
            _conductor_spec_held = True

        # Security: sanitize invalid hook keys in agent configs
        repair_agent_configs()
    finally:
        _rebuild_spec_install_admitted.reset(_rebuild_admitted_token)

    if dashboard_author_install_error is not None:
        # The independent repairs have run; now fail the rebuild. ``_wrote_out`` is left
        # UNMARKED (so the reporting wrapper returns ``wrote=False``-equivalent by never
        # appending True) and the exception propagates, which is what keeps a tightened
        # ceiling from being recorded as projected while the governed spec was not rewritten.
        raise dashboard_author_install_error

    _rebuild_answer_generation = answer_generation
    _rebuild_incomplete = False
    _invalidate_projection_if_answer_moved(answer_generation)

    if _wrote_out is not None:
        _wrote_out.append(True)
    if _held_out is not None:
        # ``True`` when a conductor spec was left on disk unwritten: its
        # ``allowedTools`` were not re-derived, so the ceiling memo must not
        # advance over it (see ``_held_out`` above). Kept apart from the guard
        # verdict: ``kirocrew.json`` above WAS written, and the dashboard's
        # model applier must not read a held conductor spec as a refused rebuild.
        _held_out.append(conductor_held)
    return path


def _lineage_unreadable_refusal(agent: str, exc: BaseException) -> str:
    """The refusal for a sidecar the gate could not read.

    Names the file and carries the parse error, so the operator repairs the
    sidecar rather than searching the agents directory. The error text is
    the reader's own (a JSON position, an OSError), never file contents. The
    only remedy offered is restoring the file: deleting it would make every
    recorded private copy read as a shared template and lose its governance,
    which is the state this gate exists to refuse.
    """
    try:
        where = str(agent_state._state_path())
    except Exception:
        where = agent_state._STATE_FILENAME
    return (
        f"cannot verify whether agent {agent!r} is a private template copy: "
        f"the lineage sidecar {where} could not be read "
        f"({type(exc).__name__}: {exc}); refusing to start a session on "
        "unverifiable permissions. Restore that file to valid JSON (the gateway "
        "log carries the same error), then retry."
    )


def _spec_unresolvable_refusal(agent: str, exc: BaseException) -> str:
    """The refusal for a spec the gate could not resolve or read.

    The other failure class: the sidecar answered, but the agents directory
    did not. Distinct wording so the two are never confused again.
    """
    return (
        f"cannot verify whether agent {agent!r} is a private template copy: "
        f"its spec could not be resolved under the agents directory "
        f"({type(exc).__name__}: {exc}); refusing to start a session on "
        "unverifiable permissions."
    )


def _stem_claimant_fork(agent: str) -> str | None:
    """The fork-backed name a direct-filename spec for *agent* declares, else None.

    Consulted only when two or more specs declare *agent*, which is when
    :func:`agent_spec_path` raises before its stem fallback is ever considered.
    The backend's own resolver accepts ``path.stem == agent`` as well as the
    declared name over an unordered directory listing, so ``<agent>.json``
    declaring a DIFFERENT name can still be the file it runs. When that name is
    a recorded private copy, the session may execute a fork's grants, and the
    gate must take the fork path for it rather than admit the ambiguity as a
    non-fork. A candidate that is unsafe or unreadable propagates: the gate
    cannot rule it out, so it fails closed like any other resolution error.
    """
    agents_dir = kiro_agents_dir_path()
    for candidate in agent_spec_candidates(agents_dir, agent):
        if not candidate.exists():
            continue
        if not _spec_path_is_safe(candidate, agents_dir):
            raise ValueError(f"direct spec candidate {str(candidate)!r} is not a safe file")
        data = _read_spec_capped(candidate)
        if not isinstance(data, dict):
            # The capped reader answers None for a spec it refused (not JSON,
            # too large, not an object). Unread, the file cannot be ruled out
            # as a fork claimant, so it is reported rather than skipped.
            raise ValueError(f"direct spec candidate {str(candidate)!r} could not be parsed")
        declared = data.get("name")
        if (
            isinstance(declared, str)
            and declared
            and declared != agent
            and agent_state.get_fork_info(declared, strict=True) is not None
        ):
            return declared
    return None


def require_fork_governance(agent: str | None, project_dir: str | Path | None = None) -> None:
    """Fail closed: block a fork-backed session start until fork governance is
    re-projected, and ABORT it when the projection failed or timed out.

    A fork's ``allowedTools``/``autoApprove`` bypass the PreToolUse gate, so a
    session consuming a fork the refresh never re-filtered would run grants the
    ceiling has since tightened away. Non-fork agents never wait and never
    raise. Raises :class:`ForkGovernanceUnresolved` only.

    *project_dir* is the cwd the backend will run with. kiro-cli resolves
    ``--agent`` against ``<cwd>/.kiro/agents`` BEFORE the global directory, so
    a checkout declaring a spec with the fork's name would have the backend
    execute the project copy — ungoverned grants included — while this gate
    validated the sanitized global one. A fork whose name is shadowed by the
    project is therefore refused outright; project shadowing of NON-fork
    agents stays the documented discovery feature and is untouched here.
    """
    if not agent:
        return
    # Each failure class gets its own refusal, because each is repaired
    # differently: a corrupt sidecar is fixed by restoring THAT file, an
    # unresolvable spec by looking at the agents directory. One message for
    # both left every operator hunting a duplicate spec that was not there.
    try:
        # strict: an unreadable sidecar must SURFACE here, not degrade to
        # "not a fork" — the lenient default would make the except branch
        # below unreachable and the guard a dead letter.
        is_fork = agent_state.get_fork_info(agent, strict=True) is not None
    except Exception as exc:
        # Unreadable lineage fails CLOSED: treating a missing/corrupt sidecar
        # read as "not a fork" would start a session whose grants predate the
        # tightened ceiling. A VERIFIED non-fork is a successful read that
        # returned no lineage — only that may pass without waiting.
        raise ForkGovernanceUnresolved(_lineage_unreadable_refusal(agent, exc)) from exc
    effective = agent
    if not is_fork:
        # Lineage is keyed by the DECLARED name, but a binding can carry
        # the file STEM where the two differ — and the backend resolves
        # that binding to the same file. Resolve before concluding "not a
        # fork"; a resolution error fails CLOSED like an unreadable sidecar.
        try:
            spec_path = agent_spec_path(agent)
        except AmbiguousAgentSpecError as exc:
            # Two or more specs declare this name. That is NOT an unverifiable
            # lineage: every one of them DECLARES `agent` (that is what the
            # ambiguity is), so the declared name is `agent` itself, whose
            # lineage was read above and found empty. Whichever of THOSE files
            # the backend runs, the verdict "not a private copy" is the same,
            # and a non-fork was never this gate's to govern. Refusing here made
            # every agent that a package installer vends twice — one upstream
            # agent reached through two dependency chains — unstartable, with
            # the duplicate regenerated on the next install. One file is not
            # among "those": the backend also matches the STEM, so a
            # `<agent>.json` declaring a fork-backed name (the resolver's stem
            # fallback, which the ambiguity discarded unread) can be the live
            # spec; it is checked here and sends the gate down the fork path.
            # Otherwise still surfaced, so the operator can tidy the directory;
            # a private copy with a same-named twin takes the fork path below,
            # where the refresh cannot pick a file to re-filter and records the
            # failure.
            spec_path = None
            try:
                stem_fork = _stem_claimant_fork(agent)
            except Exception as stem_exc:
                raise ForkGovernanceUnresolved(
                    _spec_unresolvable_refusal(agent, stem_exc)
                ) from stem_exc
            if stem_fork is not None:
                effective = stem_fork
                is_fork = True
            else:
                logger.warning(
                    "fork governance: %s; every duplicate declares the binding name, "
                    "so the non-fork verdict for %r holds for all of them",
                    exc,
                    agent,
                )
        except Exception as exc:
            raise ForkGovernanceUnresolved(_spec_unresolvable_refusal(agent, exc)) from exc
        if spec_path is not None:
            try:
                data = _read_spec_capped(spec_path)
            except Exception as exc:
                raise ForkGovernanceUnresolved(_spec_unresolvable_refusal(agent, exc)) from exc
            declared = data.get("name") if isinstance(data, dict) else None
            if isinstance(declared, str) and declared and declared != agent:
                effective = declared
                try:
                    is_fork = agent_state.get_fork_info(declared, strict=True) is not None
                except Exception as exc:
                    raise ForkGovernanceUnresolved(_lineage_unreadable_refusal(agent, exc)) from exc
    if not is_fork:
        return
    # Checked before the refresh wait: a shadowed fork is refused no matter
    # what the refresh concludes, so waiting up to the timeout first would
    # only delay the same answer. Both the binding name and the declared name
    # are checked — the backend resolves either against the project dir.
    shadow_names = project_agent_names(
        project_dir, operation="require_fork_governance", source="unknown"
    )
    if agent in shadow_names or effective in shadow_names:
        raise ForkGovernanceUnresolved(
            f"agent {agent!r} is a private template copy, but the session's "
            "project declares its own agent spec with that name; the backend "
            "would execute the project copy and bypass fork governance. "
            "Rename or remove the project's .kiro/agents spec to proceed."
        )
    if not fork_refresh._fork_refresh_settled.wait(timeout=fork_refresh._FORK_REFRESH_WAIT_SECS):
        raise ForkGovernanceUnresolved(
            f"agent {agent!r} is a private template copy and its governance "
            f"refresh did not complete within {fork_refresh._FORK_REFRESH_WAIT_SECS:.0f}s; "
            "refusing to start a session on unrefreshed permissions"
        )
    failed = fork_refresh._fork_refresh_failed
    if agent in failed or effective in failed or "*" in failed:
        raise ForkGovernanceUnresolved(
            f"agent {agent!r} is a private template copy whose governance "
            "refresh failed; refusing to start a session on stale permissions "
            "(see the gateway log for the refresh error)"
        )


def rebuild_agent_config_reporting(*, _held_out: list[bool] | None = None) -> tuple[Path, bool]:
    """:func:`rebuild_agent_config`, reporting whether it actually wrote.

    Returns ``(path, wrote)``. ``wrote`` is ``False`` exactly when the
    shared-home guard refused the write — the one non-raising path that ends
    with no spec written — and ``path`` is then the spec path that was NOT
    rewritten. ``wrote=True`` additionally requires the WHOLE rebuild to have
    returned: an exception after the write escapes instead, the memo a caller
    keeps stays behind, and the next poll rewrites — the safe direction. The
    verdict comes from the rebuild's own single guard evaluation, so no
    caller-side probe exists for a concurrent default-home boot to race.

    A conductor spec left on disk unwritten is NOT a refusal: ``kirocrew.json``
    was written, so the two consumers of ``wrote`` must not read it as one —
    the dashboard's default-model applier would announce a change that landed
    as failed and retry it forever. That hold reaches the one reader with
    something left to do, the ceiling-reprojection hook, through the private
    *_held_out* out-parameter (``rebuild_agent_config``'s, forwarded). A
    caller needing ``clean`` uses :func:`rebuild_agent_config` directly —
    neither consumer here does.
    """
    wrote_out: list[bool] = []
    path = rebuild_agent_config(_wrote_out=wrote_out, _held_out=_held_out)
    return path, bool(wrote_out and wrote_out[0])


# Backward-compat alias — callers may still use the old name.
install_agent = rebuild_agent_config


def ensure_agent_materialized(agent: str | None) -> bool:
    """Self-heal: guarantee the managed default agent config exists on disk.

    kiro-cli discovers its selectable *modes* at process startup by scanning
    ``~/.kiro/agents/*.json``. A session that spawns ``kiro chat --agent <name>``
    and then issues ``session/set_mode {modeId: <name>}`` therefore needs the
    backing file present BEFORE spawn, or kiro-cli answers
    ``-32603 "Mode '<name>' not found"`` on every turn (the crash this closes).
    Normally ``kirocrew setup --agent-only`` writes it, but a source checkout /
    dev launch that skips setup leaves it absent — this makes the runtime
    self-sufficient regardless.

    Only the managed default (``AGENT_FILENAME`` → ``kirocrew.json``) is
    regenerable here, via :func:`rebuild_agent_config`. App/custom agents are
    owned by their own subsystems, so a missing one is reported (``False``) and
    left to the caller's graceful set_mode fallback rather than being guessed at.

    Returns ``True`` when the managed default file is present (already, or after
    a regenerate); ``False`` when *agent* is non-managed or regeneration failed.
    Best-effort — never raises, so it can sit on the spawn hot path.
    """
    try:
        managed = Path(AGENT_FILENAME).stem
        if not agent or agent != managed:
            return False
        agent_file = kiro_agents_dir_path() / AGENT_FILENAME
        if agent_file.exists():
            return True
        logger.warning(
            "Managed agent config %s missing — regenerating before spawn "
            "(self-heal for kiro-cli 'Mode not found')",
            agent_file,
        )
        rebuild_agent_config()
        return agent_file.exists()
    except Exception:
        logger.warning("ensure_agent_materialized failed for agent %r", agent, exc_info=True)
        return False


def _install_aim_capabilities() -> None:
    """Write a bare ``kirocrew-lite`` agent config.

    Symbol preserved for callers (``rebuild_agent_config``).  The previous
    AIM-package install path is omitted on public installs (AIM is an
    internal package manager); the generic ``kirocrew-lite`` fallback
    config — used by the claude_code provider for cheap background work — is
    still written.
    """
    service_agents._install_lite_agent_fallback()
    service_agents._install_guest_agent()


def _install_heartbeat_agent() -> None:
    """Generate and install the kirocrew-heartbeat agent config.

    A dedicated agent for HeartbeatService.  Minimal MCP surface: the default
    spec's ``kirocrew-core`` entry (learn/spawn list and status, artifact
    reads, local_knowledge_search) plus the default-spec servers behind the
    edition's ``heartbeat_safe_tools()`` pins (none on a standalone install).  Tool approval is enforced gateway-side by
    ``_heartbeat_approval``'s read-only allowlist regardless; the per-agent MCP narrowing here
    keeps cold-start cost low and reduces the surface the gateway has to
    police.

    SEL audit logging stays at the gateway side — see
    ``GatewayOrchestrator._heartbeat_approval``.
    """
    agents_dir = kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / _HEARTBEAT_AGENT_FILENAME
    model = _background_agent_model()
    # The read of the main spec and the write of the copy are one critical section under
    # both writer locks, in the order ``_install_worker_agent`` establishes: the agents
    # lock outermost, ``kirocrew.json``'s own writer lock innermost. Without it a copy
    # derived from an older main spec can land after a newer one, and the spawn gate
    # would have verified the newer copy that kiro-cli then does not read.
    from kiro_crew.apps.bridges import _mcp_lock  # noqa: PLC0415 - boot path

    with agents_spec_lock(agents_dir), _mcp_lock():
        _write_heartbeat_spec(path, agents_dir / AGENT_FILENAME, model)
    # CC model for the heartbeat agent lives in the sidecar, not the kiro spec.
    agent_state.set_cc_model("kirocrew-heartbeat", _background_cc_model())
    logger.info("Installed heartbeat agent config: %s", path)


def _write_heartbeat_spec(path: Path, main_path: Path, model: str) -> None:
    """Derive the heartbeat spec from *main_path* and write it to *path*. Caller holds the locks."""
    # Pull the ``kirocrew-core`` entry from the main agent config so the
    # resolved command + skill-paths match the main agent (write-denied
    # commands and security still come from bundled hooks). Strip the main
    # agent's ``--include-tools``/``--include-tool-tags``/``--exclude-tools``
    # filters so all read tools surface to the heartbeat agent — security is
    # enforced gateway-side by ``_heartbeat_approval``'s read-only
    # allowlist, not by per-agent MCP filtering. Read through the
    # capped reader: a refused main spec degrades as absent, but with an
    # operator-visible signal, because the result is a heartbeat agent with no
    # MCP servers -- a worker that fails every task.
    main_config = _read_spec_capped(main_path)
    if main_config is None and main_path.exists():
        logger.warning(
            "Main agent spec %s unusable; heartbeat agent installs with no MCP servers", main_path
        )
    mcp = worker_agent.heartbeat_mcp_servers(main_config)
    # An edition's servers ride along verbatim minus ``autoApprove`` (see
    # ``heartbeat_mcp_servers``); say so where the operator reads the install log,
    # since a dropped grant otherwise shows up only as SEL ``denied`` rows.
    edition = [name for name in mcp if name not in worker_agent.HEARTBEAT_MIRRORED_SERVERS]
    if edition:
        logger.info(
            "Heartbeat agent also mounts edition MCP servers %s; their autoApprove is "
            "withheld, every call goes through _heartbeat_approval",
            ", ".join(edition),
        )

    config: dict[str, object] = {
        "name": "kirocrew-heartbeat",
        "description": (
            "Unattended polling worker — runs one HeartbeatService task per "
            "cycle with a read-only MCP toolset. Tool approval is gated "
            "gateway-side by _heartbeat_approval's read-only allowlist."
        ),
        "model": model,
        "includeMcpJson": False,
        "prompt": _HEARTBEAT_SYSTEM_PROMPT,
        "mcpServers": mcp,
        # Build from the servers actually resolved so we never reference a
        # tool namespace without a matching mcpServers entry — the
        # rebuild_agent_config flow may run before either main entry exists.
        "tools": [f"@{name}" for name in mcp],
    }

    _atomic_json_write(path, config)


def sync_aim_packages() -> None:
    """No-op on public installs (AIM package manager absent).

    Symbol preserved for callers (``rebuild_agent_config``).  AIM is an
    internal agents/skills/plugins package manager; there is nothing
    to sync across providers on a public install, so this returns immediately.
    """
    return None
