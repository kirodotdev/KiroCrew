"""Is the harness for each agent backend actually installed on THIS machine?

``acp_backends`` answers *capability* (can this build drive the harness) and
``agent_backend_governance`` answers *permission* (may this deployment select
it). Both are build- and policy-time facts, so a dashboard that gates the
backend switch on them alone offers an option that resolves nothing at spawn
time: the operator picks Claude Code, the session dies in ``session/new``, and
nothing on the page ever said which component was absent. This module answers
the third, machine-local question, and is the only one whose answer can change
without a config write or a new build.

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

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    POLICY_ID_BY_BACKEND,
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

#: How long a verdict is reused. The Claude driver shells out to mise and globs
#: the filesystem, and the dashboard polls this endpoint, so an uncached probe
#: would spawn a subprocess per poll. Module-level and read at call time (not
#: captured in a default argument) so a test can pin it to ``0`` and force a
#: re-probe without sleeping.
CACHE_TTL_SECONDS: float = 30.0

_cache: Dict[str, Tuple[float, "BackendInstallState"]] = {}
# Guards the dict only. Deliberately NOT held across a probe: ``_probe_kas``
# re-enters :func:`probe_backend` for the kiro entry, which would deadlock on a
# non-reentrant lock, and a duplicated concurrent probe costs nothing but work.
_cache_lock = threading.Lock()


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


_PROBES: Dict[str, Callable[[], BackendInstallState]] = {
    ACP_BACKEND_KIRO: _probe_kiro,
    ACP_BACKEND_KAS: _probe_kas,
    ACP_BACKEND_CLAUDE: _probe_claude,
    ACP_BACKEND_CODEX: _probe_codex,
}


def _policy_id(backend: str) -> str:
    """The policy-facing spelling, which is what the payload carries.

    The kiro backend is the empty string in code, so it cannot be its own wire
    name; ``POLICY_ID_BY_BACKEND`` owns that translation. An unregistered id
    falls back to itself rather than to ``""``, so a plugin backend still sorts
    and renders under a name.
    """
    return str(POLICY_ID_BY_BACKEND.get(backend, backend))


def clear_probe_cache() -> None:
    """Drop every cached verdict, so the next probe re-resolves."""
    with _cache_lock:
        _cache.clear()


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
    """
    cached = _cached(backend)
    if cached is not None:
        return cached

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
    "probe_backend",
    "probe_backends",
]
