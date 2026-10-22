"""The ``harness`` label: which agent harness ran the work.

Two readings, one vocabulary:

* ``kirocrew.harness`` -- a RESOURCE attribute on every series this process
  exports, holding the install's CONFIGURED default harness (global
  ``agent.acp_backend``). It answers "how many installs chose which harness"
  without touching any instrument, because a resource attribute rides every
  point already.
* ``harness`` -- a POINT attribute on ``kirocrew.session.started`` and
  ``kirocrew.turn.duration``, holding the EFFECTIVE harness of the session the
  point belongs to. A per-session or per-agent ``acp_backend`` override changes
  this one and not the resource, which is why both exist: an install whose
  default is KAS can still run a member on Codex.

**The value set is derived, never listed.** It is every id in
``agent_sdk.backends.ACP_BACKENDS_KNOWN`` in its CONFIG spelling
(:func:`~kiro_crew.agent_sdk.backends.backend_to_stored`, so kiro-cli is
``kiro`` rather than the empty runtime id), plus three values of this module's
own:

* ``claude_code`` -- the Claude Code provider seam (``agent.provider ==
  "claude_code"``). It is a different axis from ``acp_backend`` (see
  ``agent_sdk.backend_identity``), so it wins over whatever backend id the
  session reports. The seam is install-wide (``agent.provider`` has no
  per-session override), so the point attribute reads the same flag the
  resource was built from.
* ``unknown`` -- the emit point could not resolve a backend (no provider, a
  provider shape that carries none).
* ``other`` -- a backend id outside the known set, for instance one an edition
  registered. Mapping it to ``other`` keeps the set closed, whatever a config
  file or an edition holds.

A new harness added to ``ACP_BACKENDS_KNOWN`` joins the set with no edit here,
and ``test/metrics/test_harness_label.py`` pins the set to that table.

Pure and import-light: nothing here reads config, touches disk or holds a
lock, so it is safe on the turn path. The recorder build hands the configured
values in through :func:`set_configured`.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, backend_to_stored
from kiro_crew.agent_sdk.provider_identity import is_claude_code

#: The resource attribute key (configured default harness).
RESOURCE_ATTR_HARNESS = "kirocrew.harness"
#: The point attribute key (effective harness of one session).
ATTR_HARNESS = "harness"

HARNESS_CLAUDE_CODE = "claude_code"
HARNESS_UNKNOWN = "unknown"
HARNESS_OTHER = "other"

#: Every value either attribute can carry.
HARNESS_VALUES: frozenset[str] = frozenset(
    {backend_to_stored(b) for b in ACP_BACKENDS_KNOWN}
    | {HARNESS_CLAUDE_CODE, HARNESS_UNKNOWN, HARNESS_OTHER}
)

# Set by the recorder build from ``agent.provider``. Read, never written, on the
# emit paths, so a plain bool is enough: a reader that sees the old value during
# a rebuild labels one point with the setting that was live when it started.
_claude_code_seam = False


def harness_for_backend(backend: object, *, claude_code_seam: bool = False) -> str:
    """Map a runtime backend id to its closed ``harness`` value.

    ``None`` is "could not resolve" (``unknown``). A string in
    ``ACP_BACKENDS_KNOWN`` becomes its config spelling. Anything else,
    including a non-string a hand-edited config can hold, is ``other``.
    """
    if claude_code_seam:
        return HARNESS_CLAUDE_CODE
    if backend is None:
        return HARNESS_UNKNOWN
    if isinstance(backend, str) and backend in ACP_BACKENDS_KNOWN:
        return backend_to_stored(backend)
    return HARNESS_OTHER


def configured_harness(agent: object) -> str:
    """The install's default harness from an ``AgentConfig``; never raises.

    ``agent.acp_backend`` already went through the config gate
    (``resolve_selected_backend``), so it is a runtime id. An agent section that
    cannot be read is ``unknown``.
    """
    try:
        if agent is None:
            return HARNESS_UNKNOWN
        return harness_for_backend(
            getattr(agent, "acp_backend", None),
            claude_code_seam=is_claude_code(getattr(agent, "provider", None)),
        )
    except Exception:
        return HARNESS_UNKNOWN


def set_configured(agent: object) -> str:
    """Record the configured seam for the point attribute and return the resource value.

    Called by the recorder build, which already loaded the config, so the
    point attribute and the resource agree about the Claude Code seam.
    """
    global _claude_code_seam
    try:
        _claude_code_seam = bool(
            agent is not None and is_claude_code(getattr(agent, "provider", None))
        )
    except Exception:
        _claude_code_seam = False
    return configured_harness(agent)


def harness_of(provider: object) -> str:
    """The effective harness of the session *provider* serves; never raises.

    The backend is read through the SDK surface
    (``agent_sdk.drivers.acp.provider_backend``, delegating to the shape check
    ``provider_label`` uses for the session map), so the metric and the
    persisted label cannot disagree about which harness a session ran. Imported
    lazily: any process that holds a provider has already loaded that module.
    """
    if provider is None:
        return harness_for_backend(None, claude_code_seam=_claude_code_seam)
    try:
        from kiro_crew.agent_sdk.drivers.acp import provider_backend

        backend = provider_backend(provider)
        if backend is None:
            # A wrapper that is not itself an ACP shape but names the provider
            # it fronts: the background ``_ProviderBgSession`` handle, or a
            # registry session entry. One level only, so an attribute-
            # synthesizing double cannot send this walking.
            inner = getattr(provider, "provider", None)
            if inner is not None and inner is not provider:
                backend = provider_backend(inner)
    except Exception:
        backend = None
    return harness_for_backend(backend, claude_code_seam=_claude_code_seam)


def reset_for_testing() -> None:
    """Forget the configured seam."""
    global _claude_code_seam
    _claude_code_seam = False


__all__ = [
    "ATTR_HARNESS",
    "HARNESS_CLAUDE_CODE",
    "HARNESS_OTHER",
    "HARNESS_UNKNOWN",
    "HARNESS_VALUES",
    "RESOURCE_ATTR_HARNESS",
    "configured_harness",
    "harness_for_backend",
    "harness_of",
    "set_configured",
]
