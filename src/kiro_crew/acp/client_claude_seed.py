"""Author an ``AcpClient`` session's claude ``settings.local.json`` seed.

Renders the permission payload and creates, adopts, re-seeds, shares or retracts the
seed, each only while Crew can prove the file is its own.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_inline_settings``,
``_claude_local_settings_excluded``, ``_claude_settings_authored``,
``_claude_settings_claim_unrevoked``, ``_claude_settings_shared``,
``_claude_settings_written``, ``_permission_surface_share_validated``.

Instance fields read: ``_agent``, ``_model``, ``_permission_mode``, ``_seed_owner``,
``_work_dir``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry, pinned_fs
from kiro_crew.acp import seed_provenance
from kiro_crew.acp.claude_settings_sources import other_sources_define_available_models
from kiro_crew.acp.runtime_models import DEFAULT_MODEL
from kiro_crew.acp.session_mcp import session_mcp_deny_rules
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import is_sensitive_path

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def _claude_settings_usable(path: Path) -> bool:
    """Whether Crew may create *path* at all.

    ``work_dir`` is routinely a checked-out project, so this path is attacker-
    influenced: a repository can ship
    ``.claude/settings.local.json -> ~/.aws/credentials`` (or a ``.claude``
    directory that is itself a link). Crew never rewrites an existing file here,
    and reads one only for its deny rules without following a link, so the
    exposure is the CREATE: a dangling link is absent to
    ``exists()`` yet writing it materializes Crew's settings at the link's target,
    and a ``.claude`` directory that is itself a link puts the whole write
    somewhere the project does not own.

    So a symlink at either component is REFUSED rather than followed, as is a
    sensitive resolved target (the same guard
    :func:`~kiro_crew.agent_discovery._read_agent_spec` applies to agent specs).
    Refusing means exactly that: no seed. Nothing the user put there is read,
    rewritten or removed -- which is already true of every path, link or not.

    The residual is stated in the caller's warning rather than papered over: a
    session on a refused path runs without Crew's seed, so it gets no
    ``availableModels`` allowlist and no ``permissions.deny`` rules from
    ``disabledTools``. Replacing the link instead would close that at the cost of
    deleting a user's own configuration, which is not this seam's call to make.
    """
    for candidate in (path, path.parent):
        try:
            if candidate.is_symlink():
                return False
        except OSError:
            return False
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        # RuntimeError is pathlib's signal for a symlink LOOP, which a project
        # directory is one ``ln -s`` away from.
        return False
    return not is_sensitive_path(str(real))


def _share_settings_seed_if_identical(self: AcpClient, local_settings: Path, payload: str) -> bool:
    """Take the shared-reader state when the existing seed IS this client's payload.

    The relaxation of the one-live-holder rule for the case where refusing
    buys nothing: two sessions of the same agent in the same ``work_dir``
    render byte-identical settings, and before this state existed the
    second one fell to the leave-it-alone branch and ran with the whole
    ``mcpServers`` array withheld -- one session per project directory got
    Crew's tools, every sibling ran toolless. The hazard the live-holder
    rule guards against (re-seeding with a DIFFERENT
    ``permissions.defaultMode``, or unlinking the file out from under the
    owner) only exists when the payloads differ, so byte-equality is the
    exact boundary of the relaxation. Both halves are required:

    * :func:`seed_provenance.share` -- Crew's durable record names
      exactly *payload*'s bytes, checked ignoring the live holder. This is
      the provenance half: the file is Crew's own seed, not a user file
      that merely looks right.
    * :meth:`_settings_path_holds` -- the file on disk still holds those
      bytes. The record alone can describe a file a user has since
      replaced, and a replacement is theirs whatever it contains.

    A differing payload -- another agent spec, another permission mode,
    another allowlist -- fails the digest half and is refused exactly as
    before.

    The state taken is deliberately NOT ownership: no live claim is made,
    ``_claude_settings_written`` stays ``None`` and
    ``_claude_settings_authored`` stays ``False``, so this client's
    teardown neither unlinks a file the owning session is still running
    against nor pops that owner's live slot. What the sharer DOES take is a
    live registration in :func:`seed_provenance.share` -- taken BEFORE the
    byte checks, so the owner's teardown can never validate-race it -- and
    that registration is what pins the file's future: while any sharer is
    registered, the owner's teardown leaves the file in place and
    :func:`seed_provenance.claim` refuses new adoptions, so no Crew session
    can put different permission bytes at a path this session already
    delivered its MCP array against. The durable registration is withdrawn
    off-loop by ``_discard_claude_settings_seed``; ``_reset_state`` then drops
    only its in-memory half. ``_permission_surface_share_validated`` alone
    feeds the sharer half of :attr:`_permission_surface_governed`; every
    validation clears it on entry, and only full success re-earns it for the
    current bytes rather than the lease lifetime.
    """
    self._permission_surface_share_validated = False
    encoded = payload.encode("utf-8")
    owner = getattr(self, "_seed_owner", "")
    # The lease-take and its validation run under the settle lock, so they
    # are atomic against an owner teardown's move/restore transaction:
    # either this validation happens before the move (and the registration
    # it takes pins the teardown's post-move barrier), or after the whole
    # transaction settled (and the disk check sees its outcome -- the
    # restored seed, a preserved user replacement, or an empty name --
    # never the manufactured vacancy in the middle, where a racing
    # replacement could slip under an already-granted lease).
    with seed_provenance.SETTLE_LOCK:
        # Register FIRST, validate second (see seed_provenance.share): either the
        # owner's teardown sees this registration and keeps the file, or it beat
        # the registration and the disk check below fails on the unlinked path.
        if not seed_provenance.share(local_settings, payload, owner):
            return False
        if not self._settings_path_holds(
            local_settings, (len(encoded), hashlib.sha256(encoded).hexdigest())
        ):
            # Withdraw only a registration THIS validation created, mirroring
            # share()'s own rule: a sharer re-validating on a later pass keeps
            # the lease its original validation earned -- that lease is what
            # pins the file its already-delivered MCP array runs against.
            if not getattr(self, "_claude_settings_shared", False):
                if not self._withdraw_shared_reader_lease(local_settings, owner):
                    # The durable withdrawal failed, but this client never
                    # became a sharer -- its validation failed right here, so
                    # no delivered MCP array depends on the file. unshare()'s
                    # retain-on-refusal rule exists for REAL sharers; kept
                    # here it would pin the owner's seed behind a phantom
                    # lease for the process lifetime (teardown skips
                    # withdrawal when the shared flag is off). Drop the
                    # in-memory half unconditionally; the durable holder
                    # entry self-heals through process-liveness reclaim.
                    seed_provenance.unshare_local(local_settings, owner)
            return False
        self._claude_settings_shared = True
        self._permission_surface_share_validated = True
    logger.info(
        "%s already holds exactly the settings seed this session would have written "
        "(a sibling Crew session in this work dir owns it); treating the permission "
        "surface as governed for this session too, without rewriting the file.",
        local_settings,
    )
    return True


def _share_sibling_settings_seed(self: AcpClient, local_settings: Path, payload: str) -> bool:
    """Share a sibling's seed that governs this session and runs its model.

    Only the model half (``availableModels``, ``model``) may differ from
    *payload*. It grants no permission, and :meth:`_apply_startup_model` pushes
    this session's model live -- a push the adapter accepts only for an
    ``availableModels`` entry. So the seed must list *payload*'s ``model``, name none
    when *payload* names none, or, when neither carries ``availableModels``, leave the
    adapter on its own list, where the push is accepted. Its bytes are read only as the
    durable record's, and the share then validates exactly those bytes.
    """
    if self._share_settings_seed_if_identical(local_settings, payload):
        return True
    raw = self._settings_path_bytes(
        local_settings, seed_provenance.recorded_durable(local_settings)
    )
    if raw is None:
        return False
    try:
        seed_payload = raw.decode("utf-8")
        seed = json.loads(seed_payload)
    except ValueError:
        return False
    if not isinstance(seed, dict):
        return False
    own = json.loads(payload)
    model = own.get("model")
    offered = seed.get("availableModels")
    if model is None:
        runs_own_model = "model" not in seed
    elif offered is None and "availableModels" not in own:
        # Neither seed carries a list, so the adapter offers its own advertised
        # list and accepts the live push of this session's model.
        runs_own_model = True
    else:
        runs_own_model = isinstance(offered, list) and model in offered
    same_permissions = all(
        seed.get(key) == own.get(key)
        for key in seed.keys() | own.keys()
        if key not in ("availableModels", "model")
    )
    if not (runs_own_model and same_permissions):
        return False
    return self._share_settings_seed_if_identical(local_settings, seed_payload)


def _retract_reseed(
    self: AcpClient,
    local_settings: Path,
    payload: str,
    reseed_aside: "tuple[Path, tuple[int, int]]",
) -> None:
    """Undo a re-seed whose grant did not become durable.

    Takes back the just-written *payload* file -- and ONLY that file, via the
    same inode-pinned capture the write path uses, so a replacement that
    raced into the pathname is detected and preserved -- then puts the
    moved-aside prior bytes back without clobbering whatever else may hold
    the name. The prior bytes are the ones the durable record still names
    (the record for *payload* publishes only once its persist lands), so the
    path returns to exactly the recognized, repairable state it was in
    before the write: for a riding sharer, the very bytes it validated.
    """
    encoded = payload.encode("utf-8")
    taken = self._claim_pathname_if_ours(
        local_settings, (len(encoded), hashlib.sha256(encoded).hexdigest())
    )
    if taken is not None:
        with suppress(OSError):
            pinned_fs.unlink_verified_by_name(local_settings.parent, taken[0].name, taken[1])
    self._restore_aside_without_clobber(reseed_aside[0], local_settings, reseed_aside[1])


def _log_declined_share(self: AcpClient, local_settings: Path) -> None:
    """Log that an existing settings file was left authoritative."""
    logger.info(
        "%s already exists and %s; leaving it as it is. This "
        "session therefore runs without Crew's availableModels allowlist and without "
        "the permissions.deny rules from the agent spec.",
        local_settings,
        self._foreign_settings_owner(local_settings),
    )


def _render_claude_settings_payload(self: AcpClient) -> str:
    """The exact ``settings.local.json`` payload this session would write.

    Pure reads, no side effects on the path -- split out of
    :meth:`_write_claude_local_settings` so the seed path can render the
    payload BEFORE deciding whether to write it: the shared-reader check
    compares these bytes against a sibling's live seed, and the write
    branches then publish the same string. Blocking (reads the agent
    spec); it runs on the same off-loop path as the writer.
    """
    data: dict[str, Any] = {}
    perms: dict[str, Any] = {}
    if self._permission_mode:
        perms["defaultMode"] = self._permission_mode
    # Same resolution as the wire array: a project-only agent's disabledTools
    # are a restriction, and resolving only the user level would drop them.
    deny_rules = session_mcp_deny_rules(self._agent, work_dir=self._work_dir)
    if deny_rules:
        perms["deny"] = list(deny_rules)
    if perms:
        data["permissions"] = perms
    # Namespace-keyed (claude_code here), the registry index this backend's ids
    # live in — see _model_registry_namespace. Provider-ONLY: the ids the
    # backend actually advertised (cached from a prior session/new), so the seed
    # reflects what the account is served and a served-but-unregistered model
    # gets its real window. A cold cache returns nothing rather than falling
    # back to the static registry. Both keys need a warm cache. ``model`` is then
    # written whenever the session pinned one: with Crew's list beside it the
    # folded id is one of its entries, and without one the adapter resolves it
    # against its own advertised list. ``availableModels`` is written only when
    # another settings source already defines one: a specific id in Crew's list
    # would disable that family's alias wildcard (haiku would resolve to an older
    # model), which a ``model`` key never does.
    allowlist = model_registry.seed_available_models(self._model_registry_namespace)
    if allowlist:
        if other_sources_define_available_models(Path(self._work_dir)):
            data["availableModels"] = allowlist
        else:
            logger.info(
                "advertised-model cache is warm but no settings file Crew checks defines "
                "availableModels; the settings payload for %s omits availableModels "
                "so family aliases keep resolving to their current model.",
                self._claude_local_settings_path(),
            )
        # DEFAULT_MODEL ("auto") is not a provider id, and omitting the key is
        # what lets the adapter pick its default.
        if self._model and self._model != DEFAULT_MODEL:
            # Folded onto the advertised spelling HERE rather than trusting a
            # caller to have folded self._model first. The re-seed runs beside
            # the model-cache persist, which is BEFORE _apply_startup_model, so
            # depending on that fold would be an ordering coupling between two
            # distant steps -- and the failure it buys is silent (a bare id
            # names no model the adapter advertised and resolves to the base
            # window). The advertised list is non-empty here, so the cache is
            # warm and the fold is the same one _apply_startup_model and
            # set_model perform. The key is written whether or not
            # availableModels is written beside it: a model key never narrows
            # a family alias.
            data["model"] = model_registry.resolve_wire_model_id(
                self._model, self._model_registry_namespace
            )
    else:
        # Cold advertised-model cache -- the first session on this install,
        # before any session/new has been captured. Both model keys are
        # OMITTED rather than filled from the static registry, and that is the
        # fix, not a degradation: the adapter merges availableModels
        # union+dedup across settings sources, so a partial list here replaces
        # a correct provider-derived one with a stale one, and a model id that
        # matches nothing in it resolves to the base window. Writing neither
        # key leaves the adapter on its own provider list, which already
        # carries the versioned [1m] ids. This session's capture then warms the
        # cache, and the post-capture re-seed writes ``model`` (and ``availableModels``
        # when another settings source defines one).
        logger.info(
            "advertised-model cache is cold; the settings payload for %s omits "
            "availableModels/model so claude-agent-acp resolves the model from its own "
            "provider list. The re-seed after this session's model capture writes model, "
            "and availableModels too when another settings source defines one.",
            self._claude_local_settings_path(),
        )
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write_claude_local_settings(self: AcpClient) -> None:
    """Seed ``<work_dir>/.claude/settings.local.json`` for this session.

    The highest-precedence project settings source the claude-agent-acp
    adapter reads, and the only channel for the things Crew has to control:

    1. ``permissions.defaultMode`` (``self._permission_mode``). The adapter
       short-circuits its ``canUseTool`` callback ONLY for
       ``bypassPermissions``; every other mode keeps forwarding tool
       decisions to the host as ``session/request_permission``, which is what
       puts a claude session under the same gate kiro-cli sessions run under.
       Omitted when no mode was requested, leaving the adapter's own default
       (ask) rather than asserting one.
    2. ``permissions.deny``, from
       :func:`~kiro_crew.acp.session_mcp.session_mcp_deny_rules`: the agent
       spec's ``disabledTools`` cannot ride along in the ``mcpServers`` array,
       and silently dropping a restriction while forwarding the server it
       narrows would widen the tool surface.
    3. The resolved ``model`` once the backend has actually advertised a list,
       and ``availableModels`` only when, in addition, another settings source
       (user, project or managed) defines ``availableModels``. The adapter
       merges ``availableModels`` union+dedup across every settings source, so a
       user ``~/.claude`` carrying ``['opus','sonnet']`` is enough to collapse a
       versioned ``[1m]`` id (1M-token window) back to 200K -- and a
       registry-derived list seeded here does exactly the same thing to any
       model the registry has not caught up on. So on a cold advertised-model
       cache both keys are omitted (the adapter's own provider list is
       already right) and the re-seed after this session's capture fills them
       in. With a warm cache and no other source defining a list, only
       ``model`` is written: a specific id in Crew's list disables that
       family's alias wildcard, and a ``model`` key does not. See
       :func:`~kiro_crew.acp.claude_settings_sources.other_sources_define_available_models`
       and :func:`~kiro_crew.model_registry.seed_available_models`.

    **Crew touches only the file it owns.** Ownership is not the path -- a
    path under a checked-out repository is not Crew's to claim -- it is having
    CREATED the file (``_claude_settings_authored``, or the durable record in
    :mod:`kiro_crew.acp.seed_provenance` for a seed an earlier session left
    behind) AND the bytes on disk still being the ones Crew wrote. Both hold:
    overwrite by STAGE AND RENAME, which is what lets the model-substitution
    re-seed change the resolved model instead of re-sending byte-identical
    params and taking the same advisory again, without a partial write ever
    being observable at the path. Either fails: leave the path entirely alone.
    Absent: create with ``O_EXCL``, so a sibling session racing the same
    ``work_dir`` loses the create rather than clobbering the winner.

    The durable half is what makes the ownership test survive the process. A
    session killed before ``_reset_state`` leaves its seed on disk, and with a
    session-scoped test only, every later session read that orphan as a
    stranger's file and refused to touch it -- so the stale allowlist, stale
    ``model`` and stale ``permissions.defaultMode`` became permanent, and no
    amount of re-running Crew could repair them. Recognizing the orphan by
    digest lets it be re-seeded (or removed on reset) while a genuinely
    user-authored file is still left exactly as it was.

    That is what keeps this seam out of a user's project state: nothing here
    reads, merges into, rewrites or deletes a file Crew did not author, so
    there is no snapshot to take, no ownership to arbitrate between two
    sessions sharing a ``work_dir``, and no restore write on the teardown
    path.

    The cost is stated rather than hidden: a project that already carries its
    own ``settings.local.json`` gets NO seed, so that session runs without the
    ``availableModels`` allowlist and without the ``permissions.deny`` rules
    derived from ``disabledTools``, and an inherited ``bypassPermissions``
    there is not stripped. Those tool calls still reach the host gate unless
    the user's own file pre-approves them, which is the same disclosed
    boundary the inherited-``~/.claude`` gap already documents. Preserving
    such a file and restoring it afterwards is tracked separately; doing it
    here means reading and rewriting a path a checked-out repository controls.

    Blocking (writes a file); callers run it off the loop.
    """
    local_settings = self._claude_local_settings_path()
    # Re-decided on every run; only the foreign-file branch below sets it.
    self._claude_local_settings_excluded = False
    self._claude_inline_settings = None
    if not _claude_settings_usable(local_settings):
        # A symlink, or a sensitive resolved target: creating the file would
        # follow the link and write Crew's settings through it. Left entirely
        # alone, which costs this session the seed -- say what that costs
        # rather than degrade quietly.
        logger.warning(
            "%s is a symlink or resolves to a sensitive path; leaving it untouched, so "
            "this session runs without Crew's availableModels allowlist and without the "
            "permissions.deny rules from the agent spec. Remove the link to restore the "
            "session-scoped settings.",
            local_settings,
        )
        self._permission_surface_share_validated = False
        self._invalidate_session_mcp_projection()
        return
    authored = getattr(self, "_claude_settings_authored", False)
    if authored and not local_settings.exists():
        # Crew created it and something removed it. The path is free again, so
        # fall through to the create branch rather than claiming a file that
        # is not there.
        authored = False
        self._claude_settings_authored = False
        self._claude_settings_written = None
        self._invalidate_session_mcp_projection()
    if authored and not self._claude_settings_is_still_ours():
        # Created by Crew, but the bytes are not Crew's: a user replaced
        # the file atomically after the create. That file is theirs -- drop the
        # claim so reset never deletes it either.
        logger.info(
            "%s was replaced after Crew created it; leaving the replacement in place and "
            "dropping Crew's claim on it. This session therefore runs without Crew's "
            "availableModels allowlist and without the permissions.deny rules from the "
            "agent spec.",
            local_settings,
        )
        # The file is now foreign; drop the projection unconditionally.
        self._invalidate_session_mcp_projection()
        # Try the whole-record revoke first (correct when nothing else holds it).
        if seed_provenance.forget(local_settings, self._seed_owner):
            self._claude_settings_authored = False
            self._claude_settings_written = None
            return
        # forget fails closed while a live sibling sharer holds the record
        # (require_unheld). Hand back only THIS owner's claim: release drops the
        # owner holder and its in-memory _LIVE slot even while the sharer lease
        # remains, so the owner claim is not leaked for the process lifetime --
        # which would wedge every later session on this work_dir
        # (held_by_another / claim refuse it) and, once the pathname is vacant,
        # run each with mcpServers withheld.
        if seed_provenance.release(local_settings, self._seed_owner):
            self._claude_settings_authored = False
            self._claude_settings_written = None
            return
        # Neither hand-back reached the sidecar. The file is the user's by
        # OBSERVATION, so authorship -- the half _permission_surface_governed
        # reads -- drops here regardless: retaining it delivered the mcpServers
        # array under a permission file whose permissions.allow never reaches
        # session/request_permission. What is still owed is the durable owner
        # holder, and that rides its own flag for teardown to retry.
        logger.warning(
            "could not durably hand back Crew's claim on %s; dropping Crew's authorship "
            "of the replaced file now and retrying the hand-back at teardown",
            local_settings,
        )
        self._claude_settings_authored = False
        self._claude_settings_written = None
        self._claude_settings_claim_unrevoked = True
        return
    # Rendered BEFORE the ownership decision below, because the decision now
    # depends on it: a sibling's live seed that holds exactly these bytes is
    # shareable, and only the payload says whether the bytes match. Pure
    # reads, so rendering ahead of a branch that may not write costs nothing.
    payload = self._render_claude_settings_payload()
    # Set only when the live slot was taken from an ORPHAN below, so the write
    # failure handler knows whether it owes a release().
    adopted = False
    if not authored and local_settings.exists():
        # The claim is part of the DECISION, not bookkeeping after it: ownership
        # is read at a moment, so two clients starting together can both see the
        # same orphan as adoptable. Only the one that wins the live slot rewrites
        # it; the loser falls to the leave-it-alone branch below rather than
        # writing its own permission mode over a session that is already using
        # the file. The SAME fingerprint feeds the byte check and the claim, so
        # the digest the claim revalidates against the durable entry is the one
        # this client actually verified on disk -- not a second read that could
        # disagree with the first.
        adoption_fingerprint = self._expected_settings_fingerprint()
        if self._settings_path_holds(
            local_settings, adoption_fingerprint
        ) and seed_provenance.claim(
            local_settings, self._seed_owner, expect_digest=adoption_fingerprint
        ):
            # Crew's OWN seed, orphaned: a previous session (or an older Crew)
            # wrote exactly these bytes and never got to clean up -- a kill -9,
            # a crash, an app replacement. Adopt it and fall through to the
            # staged re-seed. Adoption is earned by the digest, not by the
            # path: the durable record alone proves nothing, so a user's own
            # file (or Crew's file after a user edit) still takes the
            # leave-it-alone branch below.
            #
            # This is also the only way a stale permissions.defaultMode gets
            # cleaned up. Left in place, such a file is frozen and the
            # adapter keeps reading it, so an inherited bypassPermissions
            # outlives its session indefinitely; re-seeding overwrites the mode
            # with THIS session's.
            logger.info(
                "%s holds a settings seed Crew wrote in an earlier session; re-seeding it "
                "for this session instead of leaving stale model and permission settings "
                "in place.",
                local_settings,
            )
            # LOCAL only. The instance flag is what reset reads to decide
            # whether to DELETE this path, and the durable record is what a
            # later session reads to decide whether to adopt it, so neither
            # moves until the re-seed below has actually landed: a claim taken
            # here and a write that then failed would leave reset deleting a
            # file whose bytes Crew never wrote. The live slot IS taken already
            # -- ``claim`` above is the race arbiter and has to be -- so the
            # write is wrapped below to hand it back if it does not land.
            authored = True
            adopted = True
        else:
            # Someone else's file: either the user's own project settings, or a
            # live sibling session's seed (``work_dir`` is caller-supplied and
            # every keyless client shares one default) -- including a sibling that
            # won the same orphan a moment ago. When that sibling's seed carries
            # this session's permissions and lets it run its own model, the surface
            # already governs this session and is shared rather than refused --
            # without a write, a claim, or any right to remove it later.
            shared = self._share_sibling_settings_seed(local_settings, payload)
            self._invalidate_session_mcp_projection()
            if shared:
                return
            # Crew authors none of the rest, so it touches none of them. The
            # session can still carry Crew's settings inline and leave the
            # file out; when it cannot, it runs without them and says so.
            if not self._exclude_foreign_local_settings(local_settings, payload):
                self._log_declined_share(local_settings)
            return

    # An adoption already holds the path's live slot, because ``claim`` above
    # has to be the race arbiter -- it cannot be deferred until after a
    # successful write without letting two clients both decide the same orphan
    # is theirs. So if the write does NOT land, the slot has to go back: a claim
    # kept by a client that wrote nothing makes the orphan permanently
    # unadoptable for the rest of the process (``recorded`` reports it as a live
    # session's file), and an orphan that cannot be adopted cannot be repaired
    # or deleted -- so a stale ``bypassPermissions`` in it would simply stay.
    # BaseException, not Exception: a CancelledError or a KeyboardInterrupt
    # through here wedges the slot exactly the same way. The record is left
    # alone (``release``, not ``forget``): it is what keeps the path adoptable.
    # The re-seed moves Crew's current file aside before overwriting, so this
    # holds it for the ``except`` to restore if the write does not land.
    reseed_aside: tuple[Path, tuple[int, int]] | None = None
    try:
        local_settings.parent.mkdir(parents=True, exist_ok=True)
        # BYTES on both branches, never text mode: Python's text layer rewrites
        # "\n" to "\r\n" on Windows, which would make the file on disk LARGER than
        # the payload and different from the bytes this session records. The
        # ownership check compares exact bytes, so that translation would make every
        # Windows session read as "not ours" -- the re-seed would decline, reset
        # would never remove its own file, and the MCP array would be withheld.
        # 0o600 either way.
        if authored:
            # The prior payload this session wrote (None on the adoption
            # path). Both sharer barriers below compare the whole payload
            # against it, so it is read once, up front.
            written = getattr(self, "_claude_settings_written", None)
            # A live sharer was delivered its MCP array under this file's
            # exact bytes. Any digest-changing re-seed is declined while a
            # sharer holds the file: ``seed_provenance._persist`` refuses it
            # anyway (it holds only the digest, so it cannot tell a model-key
            # refresh from a permissions change), so decline before the write
            # rather than write-then-retract. Model keys and spec edits are
            # picked up once no sibling is reading the file.
            if seed_provenance.has_sharers(local_settings):
                if written is not None and written != payload:
                    logger.info(
                        "%s is shared by a live sibling session; keeping the file the "
                        "sharer validated rather than re-seeding it. Model keys and any "
                        "spec change are picked up once no sibling is reading this file.",
                        local_settings,
                    )
                    return
            # The re-seed of a file Crew owns, STAGED AND RENAMED rather than
            # truncated in place. O_TRUNC destroyed the recorded bytes before the
            # new ones landed, so a write that failed part-way (ENOSPC, EIO, a
            # kill between truncate and write) left the adapter reading a
            # truncated settings file AND a durable record whose digest matched
            # nothing on disk -- unclaimable by every later session, which is the
            # exact failure this module exists to remove. A temp + rename leaves
            # the old, still-recorded bytes intact on failure, so the path is
            # adopted again next time. The rename replaces a link at the leaf
            # rather than refusing it (no O_NOFOLLOW to pass), which costs
            # nothing here: this branch is reached only for a path whose bytes
            # just matched Crew's record, i.e. one
            # _claude_settings_is_still_ours() read as a REGULAR file a moment
            # ago.
            #
            # INODE-PINNED: the ownership check above and this overwrite act on
            # the same PATHNAME, and a user who atomically replaced the file in
            # the gap would have their settings clobbered by the rename. Moving
            # Crew's current file aside captures it in one atomic step; the write
            # only proceeds when the MOVED inode is still Crew's, so a replacement
            # that raced in is detected and left in place instead.
            reseed_aside = self._claim_pathname_if_ours(
                local_settings, self._expected_settings_fingerprint()
            )
            if reseed_aside is None:
                logger.info(
                    "%s was replaced just before the re-seed; leaving it in place and "
                    "dropping Crew's claim rather than clobbering it. This session runs "
                    "without Crew's availableModels allowlist and without the "
                    "permissions.deny rules from the agent spec.",
                    local_settings,
                )
                # Adopted this session -> only the live slot is ours to hand back;
                # a file Crew already owned but that is now the user's -> forget the
                # durable record too, exactly as the replaced-after-create branch does.
                if adopted:
                    if not seed_provenance.release(local_settings, self._seed_owner):
                        logger.warning(
                            "could not durably release Crew's adopted claim on %s; "
                            "retaining it until a later retry or process exit",
                            local_settings,
                        )
                else:
                    if not seed_provenance.forget(local_settings, self._seed_owner):
                        # Same exit as the replaced-after-create branch: the file is
                        # observably the user's, so authorship drops now and only
                        # the un-revoked durable holder is carried for teardown.
                        logger.warning(
                            "could not durably forget Crew's settings seed at %s; dropping "
                            "Crew's authorship of the replaced file now and retrying the "
                            "hand-back at teardown",
                            local_settings,
                        )
                        self._claude_settings_claim_unrevoked = True
                self._claude_settings_authored = False
                self._claude_settings_written = None
                self._invalidate_session_mcp_projection()
                return
            atomic_write(local_settings, payload.encode("utf-8"), mode=0o600)
            # Re-checked AFTER the write, because registration races it: a
            # sharer arriving between the pre-write probes and the
            # atomic_write validated the OLD bytes. An ADOPTION stands down
            # for any sharer (claim() refused adoption while sharers
            # existed, so whoever is here arrived inside the window). An
            # owner's re-seed stands down for ANY digest change (model keys
            # or permissions): ``_persist`` refuses it under a live sharer,
            # so a rider re-seed cannot land -- restore the bytes the sharer
            # validated. Sound against a sharer arriving AFTER the write too:
            # the new bytes do not match the still-prior record, so share()
            # declines until record() publishes -- there is no third
            # interleaving.
            if seed_provenance.has_sharers(local_settings) and (
                adopted or written is None or written != payload
            ):
                self._retract_reseed(local_settings, payload, reseed_aside)
                reseed_aside = None
                if adopted:
                    logger.info(
                        "%s gained a live sharer while this session was adopting it; "
                        "restoring the seed it validated and standing down. This session "
                        "runs without Crew's availableModels allowlist and without the "
                        "permissions.deny rules from the agent spec.",
                        local_settings,
                    )
                    if not seed_provenance.release(local_settings, self._seed_owner):
                        logger.warning(
                            "could not durably release Crew's adopted claim on %s; "
                            "retaining it until a later retry or process exit",
                            local_settings,
                        )
                else:
                    # The owner keeps its live slot and its prior written
                    # payload: the restored file is still its own seed.
                    logger.info(
                        "%s gained a live sharer while a re-seed was in flight; "
                        "restoring the bytes that sharer validated. Model keys and any "
                        "spec change are picked up once no sibling is reading this file.",
                        local_settings,
                    )
                return
            # The moved old seed is deliberately KEPT until the durable record
            # for the new bytes lands below: a failing sidecar persist then has
            # the still-recorded prior bytes to put back, instead of leaving an
            # unrecorded file nothing on the host can repair.
        else:
            # Authorship of a vacant pathname is serialized against BOTH
            # live registries. A LIVE OWNER first: a sibling recreating a
            # vanished seed would have record() displace that owner's
            # slot, and the sibling's teardown would then remove a file
            # the owner still governs.
            if seed_provenance.held_by_another(local_settings, self._seed_owner):
                logger.info(
                    "%s is held by a live sibling session; declining to recreate "
                    "the seed under its claim.",
                    local_settings,
                )
                self._permission_surface_share_validated = False
                self._invalidate_session_mcp_projection()
                return
            # Then the sharer registry: a registered reader validated the
            # RECORDED bytes,
            # so a session whose payload differs may not take the vacant
            # pathname -- it would put its own permission mode under a
            # sibling's governed surface. Byte-identical re-creation stays
            # allowed: that is a sharer repairing its own vanished seed.
            if seed_provenance.has_sharers(local_settings):
                rec = seed_provenance.recorded_durable(local_settings)
                if rec is None or rec != (
                    len(payload.encode("utf-8")),
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                ):
                    self._log_declined_share(local_settings)
                    self._permission_surface_share_validated = False
                    self._invalidate_session_mcp_projection()
                    return
            # O_EXCL on the create is the whole ownership claim: if a sibling
            # session (or the user) created the file between the check above and
            # here, this raises rather than clobbering it. That is why the create
            # keeps a direct open instead of joining the branch above --
            # atomic_write publishes with a rename, which replaces whatever is at
            # the name and so cannot arbitrate a create race at all.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(local_settings, flags, 0o600)
            except FileExistsError:
                # A sibling won the create race. When its just-written seed is
                # already recorded and shareable with this payload, share it
                # exactly as the pre-existing-file branch above does. The poll
                # exists ONLY for the winner's persist still being in flight
                # (one local sidecar write, ordinarily milliseconds). It ends
                # early only once a durable record naming THIS payload's bytes
                # exists: that is the winner's record for a byte-identical
                # seed, so a share attempted AFTER seeing it either lands or
                # can never converge (the disk differs from the record). A
                # record with any OTHER digest ends nothing -- it may be a
                # stale entry a killed session left for a file since gone,
                # published before the winner's matching one -- so the poll
                # waits for the deadline, which bounds everything else. The
                # record is read BEFORE each share, never after a failed one:
                # a matching record landing between the two would otherwise
                # end the poll on a share that never saw it.
                want = (len(payload.encode("utf-8")), seed_provenance.digest(payload))
                deadline = time.monotonic() + 2.0
                while True:
                    settled = seed_provenance.recorded_durable(local_settings) == want
                    if self._share_sibling_settings_seed(local_settings, payload):
                        self._invalidate_session_mcp_projection()
                        return
                    if settled or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                self._invalidate_session_mcp_projection()
                self._log_declined_share(local_settings)
                return
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload.encode("utf-8"))
    except BaseException:
        if reseed_aside is not None:
            # The re-seed did not complete: whatever the pathname holds, take
            # back only a file whose bytes are this write's own, and put the
            # still-recorded prior bytes back without clobbering anything
            # else. The path stays the adoptable seed it was before.
            with suppress(OSError):
                self._retract_reseed(local_settings, payload, reseed_aside)
        if adopted:
            logger.info(
                "re-seed of the orphaned settings seed at %s did not land; releasing the "
                "claim so a later session can still adopt and repair it",
                local_settings,
            )
            if not seed_provenance.release(local_settings, self._seed_owner):
                logger.warning(
                    "could not durably release Crew's adopted claim on %s; retaining it "
                    "until a later retry or process exit",
                    local_settings,
                )
        raise
    # Durable half of the same claim, so the NEXT process can still recognize
    # this file as Crew's after a kill that skips the reset path.
    #
    # NOT best-effort, and taken BEFORE the instance flags rather than after.
    # An unrecorded seed is the one state nothing on the host can repair: this
    # session would still unlink it, but a kill before teardown leaves a
    # ``permissions.defaultMode`` the user never approved behind a file no later
    # session is permitted to re-seed or remove, because ownership is the record.
    # So if the grant cannot be made durable the seed is WITHDRAWN rather than
    # left behind -- the session then runs on the adapter's own defaults, which
    # is the same thing a cold advertised-model cache already does.
    if not seed_provenance.record(local_settings, payload, self._seed_owner):
        logger.warning(
            "could not durably record Crew's claim on %s; withdrawing the seed just "
            "written rather than leaving a permission mode no later session is allowed "
            "to clean up. This session runs without Crew's availableModels allowlist "
            "and without the permissions.deny rules from the agent spec.",
            local_settings,
        )
        if reseed_aside is not None:
            # A re-seed: the prior bytes are still the ones the durable record
            # names (the record for the NEW bytes publishes only once its
            # persist lands), so putting them back returns the path to exactly
            # the recognized, repairable state it was in before this write --
            # and, for a sharer riding the owner's re-seeds, the very bytes it
            # validated. No sharer can exist for the NEW bytes: a record that
            # never became durable is invisible to share().
            self._retract_reseed(local_settings, payload, reseed_aside)
            reseed_aside = None
            if adopted:
                # Back to the adoptable orphan it was; hand the slot back.
                if not seed_provenance.release(local_settings, self._seed_owner):
                    logger.warning(
                        "could not durably release Crew's adopted claim on %s; "
                        "retaining it until a later retry or process exit",
                        local_settings,
                    )
            # An owner (adopted=False) keeps its live slot and its prior
            # written payload: the restored file is still its own seed.
            return
        # O_EXCL create: settle atomically against a sibling's share validation.
        # A sharer that validated this exact payload keeps the pathname and
        # durable record; otherwise take back only the inode that still holds
        # this session's payload. A foreign replacement stays in place.
        with seed_provenance.SETTLE_LOCK:
            if seed_provenance.has_sharers(local_settings):
                logger.warning(
                    "could not durably record Crew's claim on %s; a live sibling "
                    "validated these bytes, so leaving the seed in place for it "
                    "rather than unlinking a permission file its tools were "
                    "delivered against",
                    local_settings,
                )
                if not seed_provenance.release(local_settings, self._seed_owner):
                    logger.warning(
                        "could not durably release Crew's failed seed claim on %s; "
                        "retaining it until a later retry or process exit",
                        local_settings,
                    )
                self._permission_surface_share_validated = False
                self._invalidate_session_mcp_projection()
                return
            encoded = payload.encode("utf-8")
            taken = self._claim_pathname_if_ours(
                local_settings, (len(encoded), hashlib.sha256(encoded).hexdigest())
            )
            if taken is not None:
                with suppress(OSError):
                    pinned_fs.unlink_verified_by_name(
                        local_settings.parent, taken[0].name, taken[1]
                    )
            if not seed_provenance.forget(local_settings, self._seed_owner):
                # The sidecar is unwritable, which is why we are here at all. It
                # still names the PREVIOUS digest, and the file it described is now
                # gone, so ``_persist``'s prune drops the entry on the next
                # successful write and nothing matches it in the meantime. Hand the
                # live slot back so a replacement client in this process is not
                # wedged behind a claim nobody is using.
                if not seed_provenance.release(local_settings, self._seed_owner):
                    logger.warning(
                        "could not durably release Crew's failed seed claim on %s; "
                        "retaining it until a later retry or process exit",
                        local_settings,
                    )
            # Nothing Crew governs is at the pathname now (the seed was taken back,
            # or a foreign file this session never validated stands there), so the
            # sharer half of the governance flag must not carry in from entry: a
            # validated sharer re-creating its vanished seed arrives here with it
            # still set. Same pair as the two vacant-pathname declines above -- the
            # flag, and the projection cached under it, because the cache serves
            # without re-reading the flag. The reader lease itself is retained, as
            # at those declines: it does not feed governance, and teardown
            # withdraws it only while the lease flag is set.
            self._permission_surface_share_validated = False
            self._invalidate_session_mcp_projection()
            return
    if reseed_aside is not None:
        # The grant for the new bytes is durable; the moved old seed is done.
        # Best-effort: a leftover ``.crew-gc`` is litter the next fresh seed
        # ignores, not a reason to fail a write that already landed.
        with suppress(OSError):
            pinned_fs.unlink_verified_by_name(
                local_settings.parent, reseed_aside[0].name, reseed_aside[1]
            )
        reseed_aside = None
    # Only a file Crew created AND still owns is ever overwritten or removed.
    # Set AFTER the write and after the durable record, so a failure in either
    # propagates with the claim exactly as it was: an adoption leaves no instance
    # flag for reset to act on, and a re-seed of Crew's own file leaves the
    # record describing the bytes that are still on disk.
    # A client that arrived as a SHARED READER becomes the AUTHOR here.
    # ``record`` atomically removed its reader lease in the same durable
    # transaction, so the mirrored flag moves only after that call succeeds.
    if getattr(self, "_claude_settings_shared", False):
        self._claude_settings_shared = False
    self._permission_surface_share_validated = False
    self._claude_settings_authored = True
    self._claude_settings_written = payload
    # The durable owner holder ``record`` just published IS this client's claim
    # again, so nothing is left un-revoked from an earlier foreign replace:
    # teardown's ordinary settle transaction hands it back from here.
    self._claude_settings_claim_unrevoked = False
    self._invalidate_session_mcp_projection()


async def _reseed_after_capture(self: AcpClient) -> None:
    """Re-seed settings.local.json once the backend's model list is known.

    The spawn-time seed necessarily runs BEFORE ``session/new`` --
    ``permissions.defaultMode`` and ``permissions.deny`` have to be on disk by
    the time the adapter builds its ``SettingsManager``. That is exactly why it
    cannot write the model keys on a first-ever session: the advertised-model
    cache is still cold, and the only list available to it would be a guessed
    one. So the model half of the seed lands here instead, once
    ``_capture_available_models`` has warmed that cache — which is what makes
    the file name a model actually IN the allowlist shipped beside it.

    Before this step existed the seed was written once, before capture, and
    never revisited: session 1 wrote a registry-derived list and session 2 (see
    :mod:`kiro_crew.acp.seed_provenance`) was not even allowed to correct it.

    **Called from the pre-existing ``_uses_advertised_model_selection`` branch
    beside the model-cache persist, NOT from a step of its own.** Adding a step
    to :meth:`_initialize_session` would put a new conditional and a new await
    on the first-class Kiro construction path in service of an adapter, which
    harness-parity H13 forbids however the predicate is spelled -- the test is
    not whether the Kiro path still works, it is whether it changed at all.
    Riding a branch that already exists changes no line Kiro executes, and it
    is the honest home for the work besides: this method exists BECAUSE the
    backend advertises its own model list, which is the very capability that
    branch tests.

    The two capability sets are independent opt-ins, though, so the seeding
    half is tested here rather than assumed from the caller's gate.

    Off-loop (touches disk), and a failure costs model fidelity, not the
    session.
    """
    if not self._seeds_local_settings:
        return
    try:
        await asyncio.to_thread(self._write_claude_local_settings)
    except (OSError, ValueError, TypeError):
        logger.warning("post-capture re-seed of settings.local.json failed", exc_info=True)


async def _seed_session_settings(self: AcpClient) -> None:
    """Seed this session's settings file before its launch, off the loop.

    A seed that cannot be written costs model/permission fidelity, not the
    session: the adapter falls back to its own settings sources, and tool calls
    still route through the host gate.
    """
    try:
        await asyncio.to_thread(self._write_claude_local_settings)
    except (OSError, ValueError, TypeError):
        logger.warning("initial seed of settings.local.json failed", exc_info=True)
