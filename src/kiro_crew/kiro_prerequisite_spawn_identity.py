"""Which account a spawned child loaded, and how a holder is judged against the live one.

The bracketing reads around a spawn (:func:`pre_spawn_identity`,
:func:`stamp_spawn_identity`), the comparisons the identity sweep makes against a
holder's stamp (:func:`identity_stamp_mismatch`, :func:`spawned_under`,
:func:`identity_component_dropped`), and the park grace a displaced runtime keeps.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.config.loader import CRED_KIRO_API_KEY
from kiro_crew.kiro_prerequisite_identity import (
    _combine_identity_fingerprints,
    _identity_components,
)

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


def identity_stamp_mismatch(stamp: str, live: str) -> bool:
    """Whether a child's spawn-time identity PROVABLY differs from the live one.

    ``stamp`` is the combined fingerprint recorded when the child's process
    spawned (:func:`stamp_spawn_identity`); ``live`` is a fresh read. This is
    what closes the round trip no read ever observes: seed A -> a child spawns
    under B -> the store returns to A before any poll or turn gate samples the
    interim -- the baseline and the latch both compare A to A, but the child's
    own stamp still says B.

    "Provably" is the loop guard. A retirement triggered by a transient read
    failure would recycle a healthy child, and a systematically unreadable
    component would recycle one every turn -- the respawn churn the seeded
    baseline exists to remove. So a component participates only when it is
    nonempty on BOTH sides: reads lose components under failure, they do not
    gain or alter them, so two nonempty values that differ cannot be a blip.
    An empty ``stamp`` means the spawn-time read failed or the stamping is
    unwired, and an empty ``live`` means the store cannot be read right now;
    both fall back to the existing baseline/latch machinery unchanged.

    Only the store and vault components can PROVE a mismatch. The API-key
    component cannot: a harness that strips the key (KAS) authenticates from the
    store or vault, so a key rotation changes the stamp of a child whose actual
    credential never changed. A key change is still seen by the ordinary
    baseline comparison, which retires idle sessions but spares busy children.
    """

    if not stamp or not live or stamp == live:
        return False
    was_store, _, was_vault = _identity_components(stamp)
    now_store, _, now_vault = _identity_components(live)
    return any(
        was and now and was != now for was, now in ((was_store, now_store), (was_vault, now_vault))
    )


def spawn_identity_of(holder: Any) -> str:
    """The spawn-time identity stamp recorded on *holder*, or ``""``.

    A standalone provider carries ``spawn_identity`` itself; a demuxed
    session's provider rides a shared ``AcpRuntime`` and the stamp lives on
    that runtime (``_runtime``). Empty means unstamped: the spawn-time read
    failed, disagreed with its pre-spawn read, or the stamping is unwired.
    """

    return str(
        getattr(holder, "spawn_identity", "")
        or getattr(getattr(holder, "_runtime", None), "spawn_identity", "")
        or ""
    )


def identity_component_dropped(reference: str, live: str) -> bool:
    """Whether a credential source present in *reference* is now EMPTY in *live*.

    *reference* is an identity the children provably loaded -- the retire
    baseline, or a holder's own spawn stamp -- and *live* is a fresh read. A
    source present in *reference* but gone from *live* is a credential LOSS, not
    a switch: the combined fingerprint can stay nonempty while one source
    disappears (an external ``kiro-cli logout`` with the Crew vault still
    populated), so ``bool(live)`` alone cannot see it. A loss means the children
    may be running on a credential that is gone, so the sweep must retire the
    parent and cancel them rather than spare it.

    Compared per component (store, API key, Crew vault) so a drop in any one is
    caught, unlike :func:`identity_stamp_mismatch`, which can only PROVE a change
    when a component is nonempty on both sides and so deliberately ignores a
    component that went empty. An empty *reference* answers False -- nothing was
    there to drop.
    """

    if not reference:
        return False
    return any(
        was and not now
        for now, was in zip(_identity_components(live), _identity_components(reference))
    )


def spawned_under(holder: Any, live: str) -> bool:
    """Whether *holder*'s child PROVABLY authenticated as the live account.

    The identity sweep's spare test, and the converse of
    :func:`identity_stamp_mismatch` -- deliberately stricter. A mismatch is
    proven component-wise, because a read can LOSE a component under failure
    and two nonempty values that differ cannot be a blip. Sparing a holder
    from retirement is the opposite bet: a wrong spare keeps a wrong-account
    child answering turns, so only a nonempty stamp EQUAL to a nonempty live
    fingerprint, whole, qualifies. An unstamped child is never spared -- it
    keeps exactly the pre-stamping treatment (retired by the sweep) -- and an
    unreadable store (empty *live*) spares nothing, so a host whose store
    cannot be fingerprinted keeps the fail-safe retire-everything sweep.
    The one exception to whole-string equality is the API-key component of a
    child that never received the key (a Crew-owned KAS relay, every foreign backend,
    :func:`receives_kiro_cli_api_key`): it is left out of the comparison,
    because a key rotation cannot have changed that child's credential.
    """

    if not live:
        return False
    if overlay_sets_api_key(holder):
        return False
    stamp = spawn_identity_of(holder)
    if not stamp:
        return False
    if stamp == live:
        return True
    if receives_kiro_cli_api_key(holder):
        return False
    # A harness that strips the key (Crew-owned KAS, every foreign backend) authenticated
    # from the store or vault, so the key component says nothing about its
    # credential: a key rotation alone must not un-spare it and cancel its
    # running children. Compare the components it can actually have loaded.
    keyless = _without_api_key_component(live)
    return bool(keyless) and _without_api_key_component(stamp) == keyless


def _without_api_key_component(fingerprint: str) -> str:
    """*fingerprint* with its ``+key:`` component removed."""

    store, _, vault = _identity_components(fingerprint)
    return _combine_identity_fingerprints(store, vault)


def receives_kiro_cli_api_key(holder: Any) -> bool:
    """Whether *holder*'s child is handed Kiro CLI's own ``KIRO_API_KEY``.

    The kiro-cli backend (:data:`ACP_BACKEND_KIRO`, the empty id) is, and so is
    a KAS relay spawned cli-owned; a KAS relay whose credential Crew's vault
    answers (the runtime's ``_kas_host_auth``) and every foreign backend have it
    stripped at spawn. Read off the shared runtime first -- once a provider swaps
    its placeholder client for a session provider the runtime is the only object
    that still knows the backend -- then the holder and its client. A holder
    whose backend cannot be read, and a KAS holder whose auth owner is not
    positively Crew, counts as receiving the key: that keeps the stricter
    whole-fingerprint spare, never a looser one.
    """

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    client = getattr(holder, "client", None) or getattr(holder, "_client", None)
    owners = (
        getattr(holder, "_runtime", None),
        getattr(client, "_runtime", None),
        holder,
        client,
    )
    for attribute in ("acp_backend", "backend"):
        for owner in owners:
            if owner is None:
                continue
            backend = getattr(owner, attribute, None)
            if isinstance(backend, str):
                if backend == ACP_BACKEND_KAS:
                    return getattr(owner, "_kas_host_auth", None) is not True
                return backend == ACP_BACKEND_KIRO
    return True


async def pre_spawn_identity(reader: Any) -> str:
    """Best-effort identity read taken immediately BEFORE a child spawns.

    The first half of the double read that brackets a spawn: call this right
    before ``provider.start()`` and hand the value to
    :func:`stamp_spawn_identity` as ``pre_spawn``. The child reads its
    credential between the two, so two agreeing reads bound what it can have
    loaded. Never raises; an empty return means the read failed or no reader
    is wired, and the stamp will be refused (the child keeps the pre-stamping
    protections).
    """

    if reader is None:
        return ""
    try:
        return str(await reader() or "")
    except Exception:
        logger.warning("Pre-spawn identity read failed; the stamp will be refused", exc_info=True)
        return ""


def overlay_sets_api_key(holder: Any) -> bool:
    """Whether *holder*'s per-session env overlay names :data:`CRED_KIRO_API_KEY`.

    The overlay (``extra_env``) is merged over the gateway's environment at
    spawn, so a key named there can decide which credential the child loads,
    and the gateway-side identity read cannot see it. An EMPTY value counts
    too: the child may then be refilled from the data home's ``.env`` or fall
    back to the store, and either way the gateway read cannot vouch for it.
    Every place the overlay can live is checked: the holder itself, the
    ACP client a provider wraps (``client`` / ``_client``), and the shared
    runtime either of them rides (``_runtime``) -- a cold-started provider keeps
    its overlay on its client, not on itself. Matched case-insensitively, since
    a Windows child resolves both spellings as one variable.
    """

    client = getattr(holder, "client", None) or getattr(holder, "_client", None)
    owners = (
        holder,
        getattr(holder, "_runtime", None),
        client,
        getattr(client, "_runtime", None),
    )
    for owner in owners:
        if owner is None:
            continue
        overlay = getattr(owner, "_extra_env", None)
        if isinstance(overlay, Mapping) and any(
            str(key).upper() == CRED_KIRO_API_KEY for key in overlay
        ):
            return True
    return False


async def stamp_spawn_identity(reader: Any, provider: Any, *, pre_spawn: str = "") -> None:
    """Record which identity a just-spawned child loaded, when provable.

    Called right after a kiro-backed provider's process start succeeds, with
    ``reader`` being :meth:`KiroPrerequisiteService.read_spawn_identity` (or
    ``None`` on entry points that never wire one -- the CLI, tests -- where
    this is deliberately inert) and ``pre_spawn`` the value
    :func:`pre_spawn_identity` returned immediately before ``start()``. The
    stamp lands on ``provider.spawn_identity`` and is consumed by
    ``flag_identity_stamp_mismatches`` via :func:`identity_stamp_mismatch`.

    The stamp is recorded only when the pre-spawn and post-spawn reads are
    both nonempty and EQUAL. The child reads its credential between the two,
    so agreement is what makes the record trustworthy: a single post-start
    read can miss a switch that landed before it (the child authenticated as
    B, the store returned to A, the read stamps A -- labelling a
    wrong-account child as healthy). Disagreement or a failed read refuses
    the stamp instead of guessing; both reads flow through the ordinary read
    path, so an interim account either one observes feeds the latch anyway.

    Never raises and never blocks the spawn on failure: an unstamped child is
    exactly as protected as every child was before stamping existed (the
    baseline and interim latch still cover it), so the failure direction is
    the status quo, not a regression.

    Residual (documented, not closed): a switch AND return that both complete
    strictly between the two reads, with the child sampling the interim, can
    still stamp the wrong account. Closing that needs the child to report the
    identity it actually loaded -- a protocol change, the named follow-up.
    """

    if reader is None:
        return
    if overlay_sets_api_key(provider):
        # The reader fingerprints the GATEWAY's credentials, and this child was
        # handed its own ``KIRO_API_KEY`` (a cron job's ``env`` block, say), so
        # it may be a different account entirely. Leave it unstamped: it is
        # never spared by the sweep, exactly as before stamping existed.
        logger.info("Child carries a per-session %s; left unstamped", CRED_KIRO_API_KEY)
        return
    if getattr(provider, "spawn_identity", ""):
        # First stamp wins: a warm-pool provider authenticated at FILL time,
        # and a later ``start()`` on the claim path is a no-op on an already
        # running process -- re-stamping there would relabel it with whatever
        # the store holds at claim time, which is exactly the wrong account
        # for a provider that spawned during an interim window.
        return
    try:
        stamp = await reader()
    except Exception:
        logger.warning("Spawn identity stamp read failed; child left unstamped", exc_info=True)
        return
    if not stamp or not pre_spawn:
        return
    if stamp != pre_spawn:
        # An observed switch across the spawn window: which account the child
        # loaded cannot be proven, so refuse the stamp. The differing read has
        # already fed the interim latch through the ordinary read path.
        logger.info(
            "Identity changed across a spawn window; child left unstamped "
            "(the interim latch covers the observed switch)"
        )
        return
    try:
        setattr(provider, "spawn_identity", stamp)
    except Exception:
        logger.warning("Provider refused the spawn identity stamp", exc_info=True)


#: How long a runtime displaced by the spawn-identity gate must sit parked
#: before a drain reap may kill it, even when its busy probe answers idle.
#: The probe reads registered sessions plus open init scopes, and a claim
#: opens its scope at ``create_session`` entry before the admission gate --
#: but a handful of bounded awaits (a config read, a document projection)
#: still precede the scope on the claim path, so a probe landing exactly
#: there would read a claimed runtime as idle. The grace outlasts that
#: sub-second window by two orders of magnitude; it only delays teardown of
#: an already-unclaimable process, never a claim.
IDENTITY_PARK_GRACE_SECS = 30.0


def mark_identity_parked(runtime: Any, now: float) -> None:
    """Record when the spawn-identity gate parked *runtime* on a drain list.

    The background runtime's ``agent.env`` displacement uses the same mark,
    for the same claim window. Read back by the drain reaps, which refuse to
    kill a parked runtime
    before :data:`IDENTITY_PARK_GRACE_SECS` has elapsed (see the constant for
    why). Best-effort: a runtime that refuses the attribute is reaped on the
    pre-grace rules, exactly as every parked runtime was before the grace
    existed. Backend-switch displacement never calls this, so its parks keep
    their original reap timing.
    """

    try:
        setattr(runtime, "_identity_parked_at", float(now))
    except Exception:
        logger.debug("runtime refused the identity park mark", exc_info=True)


def identity_park_grace_remaining(runtime: Any, now: float) -> float:
    """Seconds of park grace left for *runtime*; ``0.0`` when reapable.

    Zero for a runtime never marked by :func:`mark_identity_parked` (a
    backend-switch park, or one that refused the mark), so the grace never
    delays a reap the spawn-identity gate did not ask for.
    """

    parked_at = getattr(runtime, "_identity_parked_at", None)
    if not isinstance(parked_at, (int, float)):
        return 0.0
    return max(0.0, IDENTITY_PARK_GRACE_SECS - (float(now) - float(parked_at)))


def spawn_pid(child: Any) -> int | None:
    """Best-effort PID of a just-started provider or runtime.

    The spawn-identity stamp read (:func:`stamp_spawn_identity`) is a real,
    bounded suspension point between a child's process start and its
    registration in whichever structure shields it from the periodic orphan
    sweep (``_starting_pids``, the session map, a runtime registry, the warm
    pool). On hosts whose PID probe has no age grace, a sweep tick landing in
    that await would find a tracked PID in no active set and kill the healthy
    child -- so every stamp site adds this PID to the facade's
    start-to-registration guard BEFORE awaiting the stamp and discards it
    once real registration is visible (or on teardown).

    Accepts either shape: a raw ``AcpRuntime`` (its ``pid`` property) or a
    provider (``client._pid``, falling back to a live ``_proc``). Returns
    ``None`` when no live PID is extractable -- the caller then simply skips
    the shield, which is exactly the pre-stamping exposure, never worse.
    """

    try:
        pid = getattr(child, "pid", None)
        if isinstance(pid, int):
            return pid
        pid = getattr(getattr(child, "client", None), "_pid", None)
        if isinstance(pid, int):
            return pid
        process = getattr(child, "_proc", None)
        if process is not None and getattr(process, "returncode", 0) is None:
            proc_pid = getattr(process, "pid", None)
            if isinstance(proc_pid, int):
                return proc_pid
    except Exception:
        logger.debug("spawn pid probe failed", exc_info=True)
    return None
