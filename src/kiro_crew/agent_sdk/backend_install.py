"""Is the harness for each agent backend actually installed on THIS machine?

``acp_backends`` answers *capability* (can this build drive the harness) and
``agent_backend_governance`` answers *permission* (may this deployment select
it). Both are build- and policy-time facts, so a dashboard that gates the
backend switch on them alone offers an option that resolves nothing at spawn
time: the operator picks Claude Code, the session dies in ``session/new``, and
nothing on the page ever said which component was absent. This module answers
the third, machine-local question, and is the only one whose answer can change
without a config write or a new build.

**Installed is not signed in, and this module deliberately probes no credential.**
Every verdict here is about a FILE resolving; none of it says a harness can
authenticate. That gap is real -- an installed-and-signed-out harness still dies
at ``session/new`` -- but the answer does not belong here: reading another
harness's token is what the credential floor exists to forbid, and a probe that
did it would be the one reader the floor cannot fence. The sign-in answer is
declared per harness in :mod:`kiro_crew.agent_sdk.host_auth` and reaches the
operator as a remedy string the doctor row and the backend panel render
verbatim. So a caller that wants "can this harness actually run" reads a
declaration beside this state, and nothing here grows a credential probe or a
field claiming one ran.

**The resolving itself is the driver's, not this module's.** Everything that has
to reach the harness -- the binary resolves, the read of the spawn's own
process-lifetime cache, the remedy's package name -- lives in
:mod:`kiro_crew.agent_sdk.drivers.acp` and comes back as plain data. What stays
here is the contract: which states exist, which component a remedy may name, and
how long a verdict is reused. That split is the boundary this package exists to
draw, so this module imports nothing from ``kiro_crew.acp`` at any scope.

Three states, and the third is not padding. ``UNKNOWN`` means the CHECK failed
-- a resolver raised, or no probe exists for the id -- and it must never be
reported as ``MISSING``: that tells someone to install what they may already
have, and the remedy is a global npm install.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELF_SERVED_ACP,
    POLICY_ID_BY_BACKEND,
    launch_for,
)
from kiro_crew.agent_sdk.drivers import acp as acp_driver

logger = logging.getLogger(__name__)

# ── The three states ──
# Named rather than inlined: the wire spelling is pinned by the dashboard
# contract, and the payload builder, the probes and the tests must all read the
# same constant or a typo becomes a silently un-rendered state.

INSTALLED = "installed"
MISSING = "missing"
UNKNOWN = "unknown"

# ── Component names ──
# These reach the operator as "install this", so they are the names the thing is
# actually called on disk, not the internal backend ids.

COMPONENT_KIRO_CLI = "kiro-cli"
#: The ACP adapter Crew launches for the Claude backend.
COMPONENT_CLAUDE_ACP_ADAPTER = "claude-agent-acp"
#: The Claude CLI handed to that adapter as ``CLAUDE_CODE_EXECUTABLE``. A
#: separate component because the adapter's SDK does NOT search PATH for it, so
#: having one without the other is a real, distinguishable half-install.
COMPONENT_CLAUDE_CODE_CLI = "claude"

#: The codex-acp adapter. ONE component, not two: the adapter ships its own
#: compatible Codex binary, so there is no second executable Crew resolves.
COMPONENT_CODEX_ACP_ADAPTER = "codex-acp"

#: The component of a harness that serves ACP from its own binary is that binary, so
#: it is read from ``ACP_BACKEND_LAUNCH`` rather than named a second time here. ONE
#: component each, and for those harnesses that is not a simplification: there is no
#: adapter beside them to be half-installed.
#: The pi backend's TWO components: the ``pi-acp`` adapter Crew spawns, and the
#: ``pi`` agent that adapter spawns in turn. Either can be absent on its own.
COMPONENT_PI_ACP_ADAPTER = "pi-acp"
COMPONENT_PI_CLI = "pi"

#: How long a verdict is reused. The Claude driver shells out to mise and globs
#: the filesystem, and the dashboard polls this endpoint, so an uncached probe
#: would spawn a subprocess per poll. Module-level and read at call time (not
#: captured in a default argument) so a test can pin it to ``0`` and force a
#: re-probe without sleeping.
CACHE_TTL_SECONDS: float = 30.0

_cache: Dict[str, Tuple[float, "BackendInstallState"]] = {}
#: Per-backend generation, bumped by every eviction. A probe runs with the lock
#: RELEASED, so a verdict can be produced by a call that started before an
#: eviction and finish after it -- and the dashboard polls this module while the
#: re-check button evicts, which is exactly that race. The generation makes the
#: write conditional on "nothing was dropped while I was resolving", so a stale
#: answer can still be returned to its own caller but can never become the
#: answer everyone else reads. Same shape as the resolution fence in
#: ``acp.client``, for the same reason.
_probe_epoch: Dict[str, int] = {}
# Guards both dicts. Deliberately NOT held across a probe: ``_probe_kas``
# re-enters :func:`probe_backend` for the kiro entry, which would deadlock on a
# non-reentrant lock, and a duplicated concurrent probe costs nothing but work.
_cache_lock = threading.Lock()


def _epoch_locked(backend: str) -> int:
    """This backend's generation, registering the id so a later clear covers it.

    ``setdefault`` rather than ``get``: :func:`clear_probe_cache` bumps the ids it
    can see, and a first-ever probe would otherwise be invisible to it -- leaving
    the one case with no cached entry to drop as the one case a clear cannot fence.

    Caller holds :data:`_cache_lock`.
    """
    return _probe_epoch.setdefault(backend, 0)


def _bump_epoch_locked(backend: str) -> None:
    """Retire every verdict for *backend* that is already being resolved.

    Caller holds :data:`_cache_lock`.
    """
    _probe_epoch[backend] = _probe_epoch.get(backend, 0) + 1


@dataclass(frozen=True)
class BackendInstallState:
    """One backend's machine-local readiness.

    ``missing_components`` is non-empty ONLY when ``installed == MISSING``; an
    ``UNKNOWN`` verdict names nothing because the check did not get far enough
    to know what was absent.

    ``restart_required`` is the honest answer to a divergence this probe cannot
    fix: the harness is installed on disk NOW, but the running gateway already
    resolved its absence and cached that, so a session started right now would
    still fail. See :func:`_probe_claude`.
    """

    backend: str
    policy_id: str
    installed: str
    missing_components: Tuple[str, ...] = ()
    install_command: str = ""
    restart_required: bool = False


def _probe_kiro() -> BackendInstallState:
    """kiro-cli, through the resolver the spawn itself calls.

    No ``install_command``: kiro-cli is installed from its own docs, not by a
    one-liner this module could honestly print, and the first-run prerequisite
    gate already owns that remediation surface.
    """
    if acp_driver.kiro_cli_resolves():
        return BackendInstallState(ACP_BACKEND_KIRO, _policy_id(ACP_BACKEND_KIRO), INSTALLED)
    return BackendInstallState(
        ACP_BACKEND_KIRO,
        _policy_id(ACP_BACKEND_KIRO),
        MISSING,
        (COMPONENT_KIRO_CLI,),
    )


def _probe_kas() -> BackendInstallState:
    """KAS's answer IS kiro's, by construction -- there is no kas resolver.

    KAS is not an independent harness: it is reached through kiro-cli's own ACP
    relay, resolved from the same binary and handed ``acp --agent-engine v3``.
    There is no second binary, no bundle and no Node runtime of Crew's own to
    look for, so inventing a kas resolve would be a search for something that is
    never spawned -- and it could then disagree with kiro's verdict about the one
    binary they share.

    Delegating through :func:`probe_backend` rather than calling ``_probe_kiro``
    directly also means both rows share one cache entry, so listing the switch
    resolves the binary once.
    """
    kiro = probe_backend(ACP_BACKEND_KIRO)
    return BackendInstallState(
        ACP_BACKEND_KAS,
        _policy_id(ACP_BACKEND_KAS),
        kiro.installed,
        kiro.missing_components,
        kiro.install_command,
    )


def _probe_claude() -> BackendInstallState:
    """The Claude backend needs BOTH components, and names the absent one.

    The adapter is what Crew spawns; the Claude CLI is what that adapter is
    handed as ``CLAUDE_CODE_EXECUTABLE``. Either one absent is a dead backend,
    and they have different remedies, so a bare "missing" would leave the
    operator reinstalling the half they already have.

    The npm command is suggested only when the ADAPTER is what is missing.
    Nothing in this repository establishes an install command for the ``claude``
    CLI, so that half reports ``""`` rather than an invented one.

    When both halves resolve but the running gateway has a cached negative for the
    adapter, the verdict is ``INSTALLED`` with ``restart_required`` -- see
    ``drivers.acp.claude_adapter_cached_negative`` for why that beats both
    bypassing the cache and invalidating it. The opposite skew (a cached POSITIVE
    for an adapter since removed) needs no special case: the fresh resolve reports
    ``MISSING``, which is what the spawn will effectively be, since the cached
    argv now points at a path that is gone.
    """
    adapter_present, claude_cli_present = acp_driver.claude_components_resolve()

    missing: List[str] = []
    if not adapter_present:
        missing.append(COMPONENT_CLAUDE_ACP_ADAPTER)
    if not claude_cli_present:
        missing.append(COMPONENT_CLAUDE_CODE_CLI)

    policy_id = _policy_id(ACP_BACKEND_CLAUDE)
    if not missing:
        return BackendInstallState(
            ACP_BACKEND_CLAUDE,
            policy_id,
            INSTALLED,
            restart_required=acp_driver.claude_adapter_cached_negative(),
        )
    command = (
        acp_driver.claude_adapter_install_command()
        if COMPONENT_CLAUDE_ACP_ADAPTER in missing
        else ""
    )
    return BackendInstallState(
        ACP_BACKEND_CLAUDE,
        policy_id,
        MISSING,
        tuple(missing),
        command,
    )


#: Backend id → its probe. A registry rather than an ``if`` chain so an id with
#: no probe is a lookup miss that degrades to ``UNKNOWN``, instead of falling
#: through to whichever branch happened to be last.
def _probe_self_served(backend: str) -> BackendInstallState:
    """One component, named from *backend*'s launch record.

    Every harness in ``ACP_BACKEND_LAUNCH`` has the same install shape, and that is
    why one function answers for all of them: the binary that would be missing is the
    binary that serves ACP, so an absent verdict names ONE component and ONE command
    and there is no half-installed state to distinguish. The two Node adapters and pi
    each have two components and keep probes of their own.

    The component and the command both come from the record, which is what stops an
    operator being told to install something that is not what the ladder searches for
    -- the live case being a harness whose ACP package is a PLUGIN rather than the
    host that boots it.

    ``restart_required`` is read from the spawn path's own cache, like every sibling:
    the binary resolves NOW, but this process already cached its absence, so a session
    started right now still fails until the gateway restarts.
    """
    launch = launch_for(backend)
    policy_id = _policy_id(backend)
    if acp_driver.self_served_resolves(backend):
        return BackendInstallState(
            backend,
            policy_id,
            INSTALLED,
            restart_required=acp_driver.self_served_cached_negative(backend),
        )
    return BackendInstallState(
        backend,
        policy_id,
        MISSING,
        (launch.binary,),
        acp_driver.self_served_install_command(backend),
    )


def _probe_codex() -> BackendInstallState:
    """The Codex backend needs one component, and names it when it is absent.

    Without this probe the switch could render with nothing to say about a session
    that failed to start -- which was the stated reason the backend stayed out of
    ``BASELINE_SELECTABLE_BACKENDS``. The install command comes from the same
    constant the resolution ladder searches for, so the advice cannot drift from
    what would actually satisfy it.

    ``restart_required`` mirrors the claude probe: when the adapter resolves now
    but the running gateway cached a negative, the honest answer is "installed,
    restart to use it" rather than a promise the next spawn breaks.
    """
    policy_id = _policy_id(ACP_BACKEND_CODEX)
    if acp_driver.codex_adapter_resolves():
        return BackendInstallState(
            ACP_BACKEND_CODEX,
            policy_id,
            INSTALLED,
            restart_required=acp_driver.codex_adapter_cached_negative(),
        )
    return BackendInstallState(
        ACP_BACKEND_CODEX,
        policy_id,
        MISSING,
        (COMPONENT_CODEX_ACP_ADAPTER,),
        acp_driver.codex_adapter_install_command(),
    )


def _probe_pi() -> BackendInstallState:
    """The pi backend needs BOTH components, and names the absent one.

    The claude probe's shape, because the harness has the same split: the adapter
    is what Crew spawns and the agent is what the adapter spawns, and having one
    without the other is a distinguishable half-install. Unlike claude, ONE command
    installs both -- both are npm packages -- so it is suggested whichever half is
    missing.

    ``restart_required`` reads the spawn path's own caches for the same reason the
    other probes do: both components resolve once per process and never
    invalidate, so a fresh "installed" can disagree with what the next spawn does.
    """
    adapter_present, pi_present = acp_driver.pi_components_resolve()

    missing: List[str] = []
    if not adapter_present:
        missing.append(COMPONENT_PI_ACP_ADAPTER)
    if not pi_present:
        missing.append(COMPONENT_PI_CLI)

    policy_id = _policy_id(ACP_BACKEND_PI)
    if not missing:
        return BackendInstallState(
            ACP_BACKEND_PI,
            policy_id,
            INSTALLED,
            restart_required=acp_driver.pi_cached_negative(),
        )
    return BackendInstallState(
        ACP_BACKEND_PI,
        policy_id,
        MISSING,
        tuple(missing),
        acp_driver.pi_install_command(),
    )


#: Backend id -> its probe. A probe that answers by calling :func:`probe_backend`
#: for ANOTHER backend must also be named in :func:`forget_probe`, in BOTH
#: directions: its own entry is a copy, so evicting the copy alone rebuilds it from
#: the source, and evicting the source alone leaves the copy standing. ``_probe_kas``
#: is the only one today. Nothing can detect the delegation automatically -- it is a
#: call inside a function body -- so this note is the forcing function.
_PROBES: Dict[str, Callable[[], BackendInstallState]] = {
    ACP_BACKEND_KIRO: _probe_kiro,
    ACP_BACKEND_KAS: _probe_kas,
    ACP_BACKEND_CLAUDE: _probe_claude,
    ACP_BACKEND_CODEX: _probe_codex,
    ACP_BACKEND_PI: _probe_pi,
    # Every harness that serves ACP from its own binary is probed by the one function
    # above, bound to its id. Generated from the membership rather than listed, so
    # onboarding a harness of that shape adds no row here at all -- and a harness with
    # no row degrades to UNKNOWN rather than to another harness's verdict, which is
    # what a registry buys over an ``if`` chain.
    **{
        backend: functools.partial(_probe_self_served, backend)
        for backend in sorted(ACP_BACKENDS_SELF_SERVED_ACP)
    },
}


def forget_probe(backend: str) -> None:
    """Drop *backend*'s cached verdict, and every verdict coupled to it.

    Narrower than :func:`clear_probe_cache` on purpose. Re-checking one harness
    must not make the panel's next poll re-resolve all eight, and the Claude and
    self-served probes each shell out or walk the filesystem, so a wholesale clear
    would turn one button into that much work.

    KAS and KIRO are one PAIR, and the coupling runs both ways because ``_probe_kas``
    answers by calling :func:`probe_backend` for kiro -- kas's entry is a COPY of
    kiro's. Dropping only kas rebuilds it from kiro's still-cached verdict and reports
    the very answer it was asked to re-take. Dropping only kiro leaves kas holding the
    copy, so a re-check on the kiro row reports the fresh install while the kas row
    keeps saying missing, with a dead switch, for the rest of the TTL. Either
    direction alone makes the button lie about a machine it just measured, so the
    eviction travels both ways.

    Stated as branches rather than held in a table: two conditions with one reader
    each cost a reader a lookup to learn what a named condition says outright, and
    nothing can check a table anyway -- the delegation is a call inside
    ``_probe_kas``'s body. The note on :data:`_PROBES` is what tells the next author
    to come here.
    """
    with _cache_lock:
        _cache.pop(backend, None)
        # The bump travels with the pop, under one lock hold: a probe already in
        # flight for this id resolved a machine that predates whatever the caller
        # just did, so its answer must not be stored on top of the fresh one.
        _bump_epoch_locked(backend)
        if backend == ACP_BACKEND_KAS:
            # The copy's source: leaving it would rebuild the copy from it.
            _cache.pop(ACP_BACKEND_KIRO, None)
            _bump_epoch_locked(ACP_BACKEND_KIRO)
        elif backend == ACP_BACKEND_KIRO:
            # The copy: leaving it keeps a pre-install verdict on the kas row while
            # the kiro row it was copied from already reads the fresh one.
            _cache.pop(ACP_BACKEND_KAS, None)
            _bump_epoch_locked(ACP_BACKEND_KAS)


def forget_for_recheck(backend: str) -> None:
    """Drop BOTH caches that can make a fresh install read as unusable.

    This module's TTL verdict, and the running gateway's own resolve result that
    ``restart_required`` is derived from. Together they are what stands between an
    operator who just ran the install command and a working switch.

    **Non-blocking, and it must be called ON THE EVENT LOOP.** It resolves nothing --
    it only drops what is remembered -- so there is no reason to offload it, and one
    strong reason not to: :func:`drivers.acp.forget_cached_resolution` is only
    thread-safe on the loop, because every reader on the spawn path is loop-resident
    code with no ``await`` between its check and its read. Its docstring has the
    evidence.

    Deliberately NOT paired with the probe in one function. An earlier shape did
    exactly that, and bundling them is what pushed the clear into the worker thread
    the probe needs -- so the pairing was the defect, not a convenience. The caller
    clears here, then offloads :func:`probe_backend`.
    """
    acp_driver.forget_cached_resolution(backend)
    forget_probe(backend)


def _policy_id(backend: str) -> str:
    """The policy-facing spelling, which is what the payload carries.

    The kiro backend is the empty string in code, so it cannot be its own wire
    name; ``POLICY_ID_BY_BACKEND`` owns that translation. An unregistered id
    falls back to itself rather than to ``""``, so a plugin backend still sorts
    and renders under a name.
    """
    return str(POLICY_ID_BY_BACKEND.get(backend, backend))


def clear_probe_cache() -> None:
    """Drop every cached verdict, so the next probe re-resolves.

    Every generation moves with the clear, not only the ids that had an entry: an
    in-flight probe holds no entry yet, and it is the one whose write would put a
    pre-clear verdict back.
    """
    with _cache_lock:
        _cache.clear()
        for known in list(_probe_epoch):
            _bump_epoch_locked(known)


def _cached(backend: str) -> BackendInstallState | None:
    with _cache_lock:
        entry = _cache.get(backend)
    if entry is None:
        return None
    stored_at, state = entry
    if time.monotonic() - stored_at >= CACHE_TTL_SECONDS:
        return None
    return state


def probe_backend(backend: str) -> BackendInstallState:
    """This machine's readiness for *backend*, cached for ``CACHE_TTL_SECONDS``.

    Blocking: the Claude probe shells out to mise and touches the filesystem.
    Callers on an event loop must offload it.

    Never raises. A resolver that fails -- including an id with no probe at all
    -- yields ``UNKNOWN``, because the alternative is telling an operator to
    reinstall a harness whose presence was never actually determined.

    The verdict is CACHED only if nothing evicted this backend while the probe
    ran; see :data:`_probe_epoch`. The caller always gets the answer its own call
    resolved, whether or not it was stored.
    """
    cached = _cached(backend)
    if cached is not None:
        return cached

    with _cache_lock:
        epoch = _epoch_locked(backend)

    probe = _PROBES.get(backend)
    if probe is None:
        logger.debug("no install probe for agent backend %r; reporting unknown", backend)
        state = BackendInstallState(backend, _policy_id(backend), UNKNOWN)
    else:
        try:
            state = probe()
        except Exception:
            # Broad on purpose: every failure mode of a resolver that spawns a
            # subprocess and walks the filesystem is a failed CHECK, and the
            # three-state contract requires those to read UNKNOWN rather than
            # collapse into MISSING.
            logger.warning("agent backend install probe failed for %r", backend, exc_info=True)
            state = BackendInstallState(backend, _policy_id(backend), UNKNOWN)

    with _cache_lock:
        # Store only under the generation this call started in. A caller that
        # raced an eviction still gets its own answer -- it is honest about the
        # machine it looked at -- but the cache keeps the eviction's meaning, so
        # the next reader re-resolves instead of being handed a verdict taken
        # before the install the eviction was announcing.
        if _probe_epoch.get(backend, 0) == epoch:
            _cache[backend] = (time.monotonic(), state)
    return state


def probe_backends() -> List[BackendInstallState]:
    """Every known backend, sorted by ``policy_id``.

    Covers ids this build cannot serve: the switch lists all of them and has to
    be able to say which is which, so an unservable backend still needs a row
    rather than being silently absent.
    """
    return sorted(
        (probe_backend(backend) for backend in ACP_BACKENDS_KNOWN),
        key=lambda state: state.policy_id,
    )


__all__ = [
    "CACHE_TTL_SECONDS",
    "COMPONENT_CLAUDE_ACP_ADAPTER",
    "COMPONENT_CLAUDE_CODE_CLI",
    "COMPONENT_KIRO_CLI",
    "INSTALLED",
    "MISSING",
    "UNKNOWN",
    "BackendInstallState",
    "clear_probe_cache",
    "forget_probe",
    "probe_backend",
    "probe_backends",
    "forget_for_recheck",
]
