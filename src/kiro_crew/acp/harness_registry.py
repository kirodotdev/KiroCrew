"""Harness registry: which ACP harnesses exist, and whether each can run now.

The registry is the single place that answers "what harnesses are there?" for
every selection surface (chat composer, spawn tool, cron, Settings) and for the
provider factory that renders a spawn argv. It serves two populations from one
interface:

- **Bundled descriptors**, constructed in code below: kiro-cli (the default),
  KAS, Codex, and the dormant Claude Code seam. Only these may name an
  ``adapter`` — a reviewed Python entry point carrying pre-spawn /
  post-initialize steps that argv data cannot express (KAS's token resolution,
  kiro's ``~/.kiro/agents`` self-heal).
- **Operator descriptors**, parsed from ``harnesses.json`` beside the config: pure data, no
  adapter, every capability off unless explicitly enabled. The acceptance case
  is an ACP server the operator already runs and has already authenticated —
  executable, argv template, capabilities, and nothing else needed from us.

Four decisions shape this module and are worth stating before the code:

**Availability is evaluated lazily, never at boot.** ``list()`` and the
selection paths ask; nothing probes at import. Registering a harness is free, so
a Codex binary the operator has not installed costs a listing row rather than a
gateway that will not start — and kiro-cli readiness remains the only
bootstrap-gating check (R6.1). Availability is also never CACHED across calls,
which is what makes recovery work: the operator installs the binary and the next
listing says so, with no restart (R6.5).

**Availability never runs the harness.** It asks whether an executable resolves
and is a non-empty executable file, and whether the descriptor validates.
Executing a candidate to see whether it answers ACP would turn a listing into N
child processes, and an unauthenticated harness would fail that probe for a
reason ("not signed in") that is not what the listing claims to report. A
spawn-time failure is recorded through :meth:`HarnessRegistry.note_probe_failure`
instead, and expires (see :data:`_PROBE_FAILURE_TTL_SECS`).

**An invalid operator descriptor is excluded from ``list()`` and reported by
:meth:`HarnessRegistry.invalid`.** Both halves matter: a selection surface reads
``list()``, so a descriptor that failed validation is not one ``if`` away from
being handed to a session (R2.3), while the reasons stay retrievable so Settings
can show the operator what is wrong with their entry (R6.2). Everything else
stays registered — one malformed entry costs its own harness and nothing more.

**Harness definitions are not agent-writable.** A descriptor names an executable
Kiro Crew will spawn, so an agent that could author one would have arbitrary code
execution in the gateway's own identity — which makes this a three-way guard, and
each half closes a different reachable path:

- the **file-edit tool**: ``harnesses.json`` is on
  ``security._WRITE_PROTECTED_HOME_PATHS``, so an ``edit``-kind write there is
  refused while reads stay allowed;
- the **shell**: the same leaf is on ``security._WRITE_PROTECTED_BASH_LEAVES``,
  so a redirect, ``tee``, ``cp``, or any novel write verb naming the file is
  refused (the matcher is verb-independent — naming the leaf at all is the
  signal);
- the **Settings PATCH** surface (``dashboard.handlers.core._EDITABLE_CONFIG``)
  is an allowlist that never carried a harness key and must not grow one.

``kirocrew config set`` needs no bespoke guard anymore: the descriptors are not
config keys, so the setter has nothing to author or delete.

This module exposes no writer at all, and ``test_harness_registry.py`` pins all
three halves.

**Both agent write paths are closed.** Operator descriptors live in
``harnesses.json`` beside ``config.json`` — a dedicated leaf, present in BOTH
``security._WRITE_PROTECTED_HOME_PATHS`` (the file-edit tools) and
``security._WRITE_PROTECTED_BASH_LEAVES`` (shell redirects, anchored form) —
so neither an agent file-edit nor ``echo '{...}' > ~/.kiro/crew/harnesses.json``
can plant a descriptor. Reads stay allowed on both paths: the file holds no
secret and the registry (plus an operator's ``cat``) must read it freely. This
is the same relocation the repo applied to ``playwright-cli-config.json``, the
computer-use policy, and the OMC rotation record: a value that is an input to a
security decision moves OUT of the generally-writable config surface instead of
growing a bespoke guard. The registry still validates every row as untrusted —
an entry that fails validation is excluded from every listing with a recorded
reason — because write protection bounds who can author the file, not what a
legitimate author may have typo'd.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

# ``resolve_executable`` is the adapters' seam, imported into this namespace
# rather than reimplemented: an adapter owns its own command resolution
# (kiro-cli's install-location chain, KAS's Node-plus-script pair) and a
# descriptor with no adapter gets the generic PATH rule, which is all an operator
# harness needs. Availability calls it as a module global, so one seam answers
# for every harness and a test can substitute it once.
from kiro_crew.acp.harness_adapters import resolve_executable
from kiro_crew.acp.harness_descriptor import (
    ADAPTER_CLAUDE,
    ADAPTER_KAS,
    ADAPTER_KIRO,
    MCP_DELIVERY_FILE_FED,
    MCP_DELIVERY_WIRE_FED,
    CapabilitySet,
    HarnessDescriptor,
    descriptor_from_mapping,
    validate_descriptor,
)

#: Leaf name of the operator-descriptor file, resolved against ``config_dir()``.
#: A module-level constant (not an inline literal) because ``security.py`` names
#: the same leaf in BOTH write-protection registers and the docs name it for
#: operators — one spelling, pinned by ``test_harness_registry``.
OPERATOR_HARNESSES_LEAF = "harnesses.json"

logger = logging.getLogger(__name__)

# ── Bundled harness ids ──
# Kiro-cli's id is spelled out rather than reusing ``ACP_BACKEND_KIRO`` (the
# empty string): an id appears in a config key, an API query parameter, a session
# record, and a spawn argument, and "" is unusable in all four. The empty string
# remains its ALIAS — see :data:`_ALIASES`.

HARNESS_KIRO = "kiro"
HARNESS_KAS = "kas"
HARNESS_CODEX = "codex"
HARNESS_CLAUDE = "claude"

#: The harness an operator who configures nothing gets, and the one an unusable
#: configured value degrades to (harness-parity H1/H3).
DEFAULT_HARNESS = HARNESS_KIRO


class UnknownHarness(LookupError):
    """Raised for a harness id that is not registered.

    ``LookupError`` because that is what the operation is — a failed lookup — so
    a caller that already handles ``KeyError``-shaped failures keeps working.
    """


class HarnessUnavailable(RuntimeError):
    """Raised when a registered harness cannot serve a session right now.

    Distinct from :class:`UnknownHarness` on purpose: "you named something that
    does not exist" and "that harness exists but its binary is missing" are
    different operator actions, and a surface that collapses them tells the
    operator to fix the wrong thing.
    """

    def __init__(self, harness_id: str, reason: str) -> None:
        super().__init__(f"harness {harness_id!r} is unavailable: {reason}")
        self.harness_id = harness_id
        self.reason = reason


@dataclass(frozen=True)
class HarnessListing:
    """One row of :meth:`HarnessRegistry.list` — what a surface needs to render.

    ``reason`` is empty exactly when ``available`` is True, so a surface can show
    it unconditionally. ``valid`` is False only for the rows
    :meth:`HarnessRegistry.invalid` returns; ``list()`` never yields one.
    """

    id: str
    display_name: str
    available: bool
    reason: str
    bundled: bool
    valid: bool = True


# ── Bundled descriptors ──
# Capability sets are transcribed from the ``ACP_BACKENDS_*`` frozensets in
# acp/types.py and from the code paths that gate Tool Search and reasoning
# effort. Every membership below is a deliberate decision with the evidence in
# its comment; a capability nobody has demonstrated stays off (harness-parity
# H6), because the failure direction of a wrong grant is silent.

_KIRO_DESCRIPTOR = HarnessDescriptor(
    id=HARNESS_KIRO,
    display_name="Kiro CLI",
    executable="kiro-cli",
    # kiro-cli's spawn command is rendered from this template:
    # ``[kiro_bin, "acp", "--agent", agent]``, plus ``["--model", model]`` when a
    # model is pinned. A golden test pins those bytes, because the default path's
    # argv is the one thing no operator opted into and every install depends on.
    argv=("{executable}", "acp"),
    agent_args=("--agent", "{agent}"),
    model_args=("--model", "{model}"),
    capabilities=CapabilitySet(
        # Every grant kiro-cli holds now lives on THIS descriptor (the
        # ACP_BACKENDS_SESSION_SHARING / _STEER / _INTERNAL_SANDBOX / _ACP_RUNTIME
        # / _KIRO_IDENTITY_STORE frozensets that once all contained
        # ACP_BACKEND_KIRO are retired — wave-2 T5).
        session_sharing=True,
        steer=True,
        internal_sandbox=True,
        acp_runtime_pool=True,
        kiro_identity_store=True,
        # Tool Search is a kiro-cli feature written into its own workspace
        # ``cli.json`` overlay (``providers.acp._write_tool_search_overlay``).
        mcp_tool_search=True,
        # Effort reaches kiro-cli through the same overlay
        # (``providers.acp._apply_effort_overlay``) plus its advertised
        # ``configOptions``.
        reasoning_effort=True,
    ),
    # kiro-cli reads its MCP servers from its own agent spec; ``session/new``
    # carries only the pooled broker stubs, which shadow same-named spec entries.
    mcp_delivery=MCP_DELIVERY_FILE_FED,
    adapter=ADAPTER_KIRO,
)

_KAS_DESCRIPTOR = HarnessDescriptor(
    id=HARNESS_KAS,
    display_name="Kiro Agent Server",
    # KAS is reached through kiro-cli's OWN ACP relay -- ``kiro-cli acp
    # --agent-engine v3 --auth-method cli`` -- so the executable it resolves is
    # kiro-cli, discovered through the same candidate chain the kiro harness uses.
    # The relay owns both concerns Crew used to hold: locating KAS's extracted
    # Node bundle, and answering its access-token callback. ``kas_transport``
    # builds the argv; neither an ``--agent`` nor a ``--model`` block appears
    # because both are chosen per session over the wire.
    executable="kiro-cli",
    argv=("{executable}",),
    capabilities=CapabilitySet(
        # The grant now lives on this descriptor (the ACP_BACKENDS_* views are
        # retired — wave-2 T5). KAS keeps steer + acp_runtime_pool; it
        # deliberately does NOT get session_sharing (its teardown deletes the
        # persisted session, so a shared subagent would strand spawn_continue)
        # and does NOT get internal_sandbox.
        steer=True,
        acp_runtime_pool=True,
        # NOT granted, even though the process on the end of the argv IS
        # kiro-cli. The relay starts the KAS server with no ``--sandbox``
        # argument and KAS's sandbox factory resolves an absent config to its
        # no-op backend, so no OS sandbox starts inside. This capability makes
        # ``sandbox.wrap_argv`` SKIP Crew's own seatbelt in favour of the
        # harness's internal one, so claiming it here would trade a real layer
        # for one that never starts (harness-parity H7 -- the flag that fails
        # OPEN).
        internal_sandbox=False,
        # GRANTED: ``--auth-method cli`` keeps token resolution inside the
        # kiro-cli process, which holds the OIDC refresh token, so every access
        # token a KAS session serves comes from kiro-cli's own identity store.
        # That is the demonstration this capability waits for. It authorizes
        # retiring a live session's child when the store starts naming a
        # different account: without it a KAS session would keep serving turns on
        # a logged-out account's credentials.
        kiro_identity_store=True,
        # NOT granted, and both are a deliberate WITHDRAWAL of a write KAS
        # receives today. The gates used to be spelled ``is_claude_backend``, so
        # KAS got kiro-cli's ``cli.json`` overlay writes as a consequence of a
        # negative test rather than of anything it demonstrated (harness-parity
        # H6). Tool Search and the effort overlay are both written into kiro-cli's
        # OWN workspace ``cli.json``; the relay does not forward that file's
        # settings to the v3 engine, so the writes bought nothing and cost a
        # settings file inside the user's project directory. Effort is the one
        # with a second channel (the live ``/effort`` command), and it is
        # withdrawn too rather than half-kept: the spawn path already drops a
        # per-spawn effort level for KAS and reports the drop, so granting it here
        # would make one build answer "does KAS honour effort" two different ways.
        # Either opts in the moment someone shows KAS honours the channel.
        mcp_tool_search=False,
        reasoning_effort=False,
    ),
    mcp_delivery=MCP_DELIVERY_WIRE_FED,
    adapter=ADAPTER_KAS,
)

_CODEX_DESCRIPTOR = HarnessDescriptor(
    id=HARNESS_CODEX,
    display_name="Codex CLI",
    # The ACP server is the ``codex-acp`` npm adapter, NOT the ``codex`` CLI:
    # per the dormant seam's own spawn path (``_resolve_codex_acp_bin`` in
    # acp/client.py, upstream #7813), "The 'codex' CLI alone does not serve
    # ACP" — it would read ``acp`` as a prompt and never answer initialize, so
    # every selected session would hang at handshake. The adapter is spawned
    # bare and driven entirely over the pipe (it ships its own Codex binary),
    # and model selection travels over ``session/set_config_option`` (codex is
    # in the config-option channel; STANDARD profile), so there are no
    # model_args either.
    executable="codex-acp",
    # Data-only: no adapter class exists for Codex yet, so this entry is exactly
    # what an operator descriptor would be, validated structurally only (the
    # binary is not present in CI). A harness whose real convention differs is
    # corrected here, in data, with no spawn-code change.
    argv=("{executable}",),
    capabilities=CapabilitySet(),
    mcp_delivery=MCP_DELIVERY_WIRE_FED,
)

_CLAUDE_DESCRIPTOR = HarnessDescriptor(
    id=HARNESS_CLAUDE,
    display_name="Claude Code",
    executable="claude-agent-acp",
    argv=("{executable}",),
    capabilities=CapabilitySet(
        # Grants all withdrawn on THIS descriptor (the ACP_BACKENDS_* sets it
        # once sat in — or out of — are retired, wave-2 T5): the seam runs one
        # AcpClient per session, so no runtime pool and no session sharing, and
        # it authenticates through its own subscription rather than kiro-cli's
        # identity store.
        # Effort IS supported, but as a live push after the session is ready
        # (``AcpProvider._set_claude_effort`` -> ``session/set_config_option``)
        # rather than through the kiro cli.json overlay.
        reasoning_effort=True,
    ),
    mcp_delivery=MCP_DELIVERY_WIRE_FED,
    adapter=ADAPTER_CLAUDE,
)

#: Bundled descriptors in listing order, default first.
BUNDLED_DESCRIPTORS: tuple[HarnessDescriptor, ...] = (
    _KIRO_DESCRIPTOR,
    _KAS_DESCRIPTOR,
    _CODEX_DESCRIPTOR,
    _CLAUDE_DESCRIPTOR,
)

#: The bundled descriptors keyed by id, for the reads that must not touch
#: configuration. Built once at import from the same tuple, so it cannot name a
#: harness the listings do not.
_BUNDLED_BY_ID: Mapping[str, HarnessDescriptor] = {d.id: d for d in BUNDLED_DESCRIPTORS}

#: Bundled harnesses that this BUILD cannot serve at all, mapped to the reason a
#: listing shows. Empty after upstream #7301 made Claude Code a selectable public
#: backend: the public build genuinely serves it (``acp/client.py`` owns the whole
#: spawn path and the adapter is a public npm package), so claude's row moved out
#: of here. Whether claude is USABLE on a given machine is a separate question,
#: answered by executable resolution today (and by the install probe once stage 3
#: wires ``agent_sdk.backend_install`` in) — a missing ``claude-agent-acp`` yields
#: an "unavailable" listing with the binary reason, not an "unserviceable" one.
#:
#: The mechanism is retained deliberately: an edition whose build genuinely cannot
#: serve a bundled harness registers its refusal here, so the harness stays visible
#: with an honest reason rather than silently spawning something else under its
#: label. It is a build-capability gate, distinct from the deployment-policy gate
#: (``acp_backends.selectable_backends()``) and the machine-availability gate.
_UNSERVICEABLE: Mapping[str, str] = {}


def unserviceable_reason(harness_id: str) -> str:
    """The ``_UNSERVICEABLE`` reason for ``harness_id``, or ``""`` when it serves.

    The single read of the ``_UNSERVICEABLE`` map for callers outside this
    module (``acp.harness_selection.unserviceable_reason``, which the selection
    surfaces consume). Keeping the map private and exposing it only through this
    function keeps the ownership posture one-directional at the seam: a consumer
    asks about one harness and cannot enumerate or mutate the table. After wave 2
    and upstream #7301 the map is EMPTY in the public build — Claude Code is now a
    selectable public backend, so it no longer sits here — and a harness that
    merely lacks a legacy ``acp_backend`` spelling (Codex, every operator
    descriptor) was never here either; all of them answer ``""``. An edition whose
    build genuinely cannot serve a bundled harness is what would populate it.
    """
    return _UNSERVICEABLE.get(harness_id, "")


# ── Legacy ``agent.acp_backend`` aliases ──
# Upstream selects a backend at ``agent.acp_backend``; the empty string means
# kiro-cli. Those values keep working unchanged by resolving to descriptor ids
# (R1.6). Resolution is TOTAL and degrades to the default for anything else,
# mirroring ``_normalize_acp_backend``: an unknown or unselectable persisted
# value costs a log line, never a gateway that will not start (harness-parity
# H1/H3).
_ALIASES: Mapping[str, str] = {
    "": HARNESS_KIRO,
    HARNESS_KIRO: HARNESS_KIRO,
    HARNESS_KAS: HARNESS_KAS,
    HARNESS_CODEX: HARNESS_CODEX,
    HARNESS_CLAUDE: HARNESS_CLAUDE,
}

#: How long a recorded spawn/initialize failure keeps a harness marked
#: unavailable. It expires rather than sticking, because the failure is a
#: SNAPSHOT of one attempt: the operator signs in, or reinstalls the binary, and
#: nothing would clear a permanent verdict short of a gateway restart — which
#: R6.5 exists to avoid. Short enough that a repaired harness heals on its own,
#: long enough that a listing does not re-offer a harness that just failed.
_PROBE_FAILURE_TTL_SECS = 300.0


class HarnessRegistry:
    """Serves harness descriptors, availability, and legacy alias resolution.

    Synchronous throughout: every answer is either in-memory data or a stat-level
    filesystem question, so an async wrapper would add a hop without removing a
    blocking call. Callers on the event loop that care route the listing through
    ``asyncio.to_thread`` the same way the config readers do.

    One process-wide instance is expected (:func:`registry`), so the descriptor
    cache is guarded by a lock: listings are reached from request handlers and
    from ``to_thread`` offloads at the same time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprint: tuple | None = None
        self._descriptors: dict[str, HarnessDescriptor] = {}
        self._invalid: dict[str, list[str]] = {}
        self._configured_default: str = ""
        self._probe_failures: dict[str, tuple[float, str]] = {}

    # ── Loading ──

    def _operator_section(self) -> tuple[dict, str]:
        """``(operator descriptors, agent.default_harness)`` as stored.

        The descriptors are read from ``harnesses.json`` beside ``config.json``
        — a DEDICATED file, not a config key, because a descriptor names a
        binary the gateway spawns and no loader clamp can bound that value. A
        dedicated leaf can be write-protected on BOTH agent write paths
        (``security._WRITE_PROTECTED_HOME_PATHS`` for the file-edit tools and
        ``security._WRITE_PROTECTED_BASH_LEAVES`` for shell redirects) without
        touching how ``config.json`` itself is read, which is the exposure that
        made the earlier ``agent.harnesses`` config key unshippable: the config
        file deliberately stays bash-readable, so a raw ``echo '{...}' >
        config.json`` could plant a shape-valid descriptor there. Same
        relocation the repo already applied to ``playwright-cli-config.json``,
        ``computer_use.json``, and the OMC policy for this exact class.

        ``default_harness`` stays a config key: it selects among already
        registered ids and an unknown or unavailable value degrades to
        kiro-cli, so the loader-clamp reasoning that protects the rest of the
        config holds for it.

        Deferred import of the config loader, for the reason
        ``_normalize_acp_backend`` defers the reverse edge: the loader is imported
        first by the gateway and desktop entrypoints, and a module-scope import
        here would put the whole config module behind an ACP vocabulary import.
        A config that cannot be read at all leaves the bundled harnesses
        serving — one broken file must not cost the default harness; a
        ``harnesses.json`` that is absent (the common case) or unparseable
        likewise costs only the operator rows.
        """
        default_harness = ""
        try:
            from kiro_crew.config.loader import KiroCrewConfig

            agent = KiroCrewConfig.load().agent
            default_harness = getattr(agent, "default_harness", "") or ""
        except Exception:
            logger.warning("harness registry: config unreadable; default harness is kiro")
        raw: Any = {}
        try:
            from kiro_crew.config.loader import config_dir

            path = config_dir() / OPERATOR_HARNESSES_LEAF
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "harness registry: %s unreadable; serving bundled harnesses only",
                OPERATOR_HARNESSES_LEAF,
            )
            raw = {}
        return (raw if isinstance(raw, dict) else {}), str(default_harness)

    @staticmethod
    def _fingerprint_of(raw: Mapping[str, Any], default_harness: str) -> tuple:
        """A cheap, total signature of the operator harness configuration.

        Keyed on the CONTENT rather than on the config file's mtime because that
        is what the parse result depends on: an unrelated settings write must not
        re-validate every operator descriptor, and an edit to one must.
        ``repr`` is used rather than JSON so an unserializable value (config is
        untrusted) still produces a key instead of raising.
        """
        return (default_harness, tuple(sorted((str(k), repr(v)) for k, v in raw.items())))

    def _ensure_loaded(self) -> None:
        """Refresh the descriptor cache when the operator configuration changed."""
        raw, default_harness = self._operator_section()
        fingerprint = self._fingerprint_of(raw, default_harness)
        with self._lock:
            if self._fingerprint == fingerprint and self._descriptors:
                return
            descriptors: dict[str, HarnessDescriptor] = {d.id: d for d in BUNDLED_DESCRIPTORS}
            invalid: dict[str, list[str]] = {}
            for key, value in raw.items():
                harness_id = str(key)
                descriptor, reasons = descriptor_from_mapping(
                    value,
                    harness_id=harness_id,
                    taken_ids=tuple(descriptors),
                )
                if descriptor is None:
                    # One bad entry costs its own harness and nothing else: the
                    # loop keeps going, the reasons are retained for the operator,
                    # and every other harness stays selectable (R2.3).
                    invalid[harness_id] = reasons
                    logger.warning(
                        "harness registry: ignoring operator harness %r (%s)",
                        harness_id,
                        "; ".join(reasons),
                    )
                    continue
                descriptors[descriptor.id] = descriptor
            self._fingerprint = fingerprint
            self._descriptors = descriptors
            self._invalid = invalid
            self._configured_default = default_harness

    def reload(self) -> None:
        """Drop the cached descriptors so the next call re-reads configuration.

        For the write paths that persist a harness definition (and for tests):
        the content fingerprint already catches an edit, so this only shortens
        the window, and it is deliberately the ONLY mutator this class exposes —
        there is no setter for a harness definition anywhere in this module.
        """
        with self._lock:
            self._fingerprint = None
            self._descriptors = {}
            self._invalid = {}
            self._configured_default = ""

    # ── Reads ──

    def get(self, harness_id: str) -> HarnessDescriptor:
        """The descriptor for ``harness_id``.

        Raises :class:`UnknownHarness` — including for an operator descriptor
        that failed validation, which is registered nowhere. Availability is a
        separate question; see :meth:`require_available`.
        """
        self._ensure_loaded()
        descriptor = self._descriptors.get(harness_id)
        if descriptor is None:
            known = ", ".join(sorted(self._descriptors))
            raise UnknownHarness(f"unknown harness {harness_id!r} (registered: {known})")
        return descriptor

    def default(self) -> HarnessDescriptor:
        """The default harness: ``agent.default_harness``, else kiro-cli.

        A configured default that is unknown or unavailable degrades to kiro-cli
        with a logged reason rather than raising, for the reason H1/H3 give: an
        operator whose configuration is unusable still gets a working gateway.

        Reads ``agent.default_harness`` ONLY — the legacy ``agent.acp_backend``
        is not consulted here, and composing
        ``resolve_alias(config.agent.acp_backend)`` is the caller's
        responsibility. A consumer wiring session creation MUST compose the two
        (an explicit ``default_harness`` first, else the resolved alias) for an
        operator's stored ``acp_backend`` to keep selecting the harness it names;
        without that composition a config carrying only ``acp_backend: "kas"``
        silently starts sessions on kiro-cli. Keeping the composition out of
        here is deliberate: the precedence between the two keys belongs to the
        surface that knows which one the operator meant, and this method stays
        the single answer to "what does ``default_harness`` say".
        """
        self._ensure_loaded()
        configured = self._configured_default
        if configured:
            try:
                descriptor = self.get(configured)
            except UnknownHarness:
                logger.warning(
                    "Ignoring agent.default_harness %r (unknown harness); using %r",
                    configured,
                    DEFAULT_HARNESS,
                )
            else:
                available, reason = self._availability(descriptor)
                if available:
                    return descriptor
                logger.warning(
                    "Ignoring agent.default_harness %r (%s); using %r",
                    configured,
                    reason,
                    DEFAULT_HARNESS,
                )
        return self.get(DEFAULT_HARNESS)

    def capabilities(self, harness_id: str) -> CapabilitySet:
        """The capability set ``harness_id`` declares."""
        return self.get(harness_id).capabilities

    def bound_capabilities(self, harness_id: str) -> CapabilitySet:
        """Capabilities for a harness a session is already bound to. Non-blocking.

        The read every capability GATE uses, and it is separate from
        :meth:`capabilities` for one reason: a gate is asked from the event loop —
        the identity-change sweep walks every live session, ``supports_steer`` is
        consulted mid-turn — and :meth:`capabilities` goes through
        :meth:`_ensure_loaded`, which stats and may read ``config.json``. This
        answers from the loaded cache when there is one and from the bundled table
        otherwise, so no gate puts a configuration read on the loop.

        A harness this cannot resolve answers with every flag OFF rather than
        raising. Fail-closed is the correct direction for a gate — the feature
        does not happen, instead of a harness inheriting a sandbox waiver or
        session sharing it never declared — and raising would turn an
        unresolvable id into a broken turn. It is a floor and not a path: an
        operator harness is resolvable from the cache from the moment a surface
        selected it (selection loads the registry), and a bundled one needs no
        cache at all. The warning is what makes the floor diagnosable, since a
        silently capability-less session presents only as a feature that stopped
        happening.
        """
        with self._lock:
            descriptor = self._descriptors.get(harness_id)
        if descriptor is None:
            descriptor = _BUNDLED_BY_ID.get(harness_id)
        if descriptor is None:
            logger.warning(
                "harness registry: no descriptor for bound harness %r; "
                "answering every capability as unsupported",
                harness_id,
            )
            return CapabilitySet()
        return descriptor.capabilities

    def resolve_alias(self, acp_backend_value: object) -> str:
        """A legacy ``agent.acp_backend`` value as a registered descriptor id.

        Total, and never raises: every value ``_normalize_acp_backend`` accepts
        today maps to a registered bundled id, and everything else — an unknown
        string, a non-string a hand-edited config can hold, or a value the code
        knows but cannot serve — resolves to the default. That is the same
        degrade-to-default posture the normalizer has, and it is deliberate: a
        typo must not select a foreign harness, and it must not stop the gateway
        either (harness-parity H3).
        """
        if isinstance(acp_backend_value, str):
            # Unserviceable is consulted BEFORE the alias table, and the order is
            # the whole safety of this function: nothing structurally keeps the
            # two mappings disjoint (a harness that gains a legacy identifier
            # gains an alias row, while its unserviceable row lives elsewhere in
            # the module), and with the alias first an id present in both would
            # resolve to a harness that cannot serve a session — which is exactly
            # what the unserviceable row exists to prevent.
            unserviceable = _UNSERVICEABLE.get(acp_backend_value)
            if unserviceable:
                logger.warning(
                    "agent.acp_backend %r names a harness that cannot serve a session (%s); "
                    "using %r",
                    acp_backend_value,
                    unserviceable,
                    DEFAULT_HARNESS,
                )
                return DEFAULT_HARNESS
            resolved = _ALIASES.get(acp_backend_value)
            if resolved is not None:
                return resolved
        if acp_backend_value not in (None, ""):
            logger.warning(
                "Ignoring agent.acp_backend %r (unknown harness); using %r",
                acp_backend_value,
                DEFAULT_HARNESS,
            )
        return DEFAULT_HARNESS

    # ── Availability ──

    def availability(self, harness_id: str) -> tuple[bool, str]:
        """``(available, reason)`` for ``harness_id``; ``reason`` is empty when available."""
        return self._availability(self.get(harness_id))

    def _availability(
        self, descriptor: HarnessDescriptor, *, honor_recent_failure: bool = True
    ) -> tuple[bool, str]:
        """``(available, reason)`` for an already-resolved descriptor.

        Three questions, cheapest first: is the harness serviceable at all, did a
        recent spawn fail, and does its executable resolve? Nothing here runs the
        harness (see the module docstring), and nothing is cached — the answer
        describes the machine as it is right now, which is what lets a newly
        installed binary appear without a restart (R6.5).

        Takes the descriptor rather than an id so a listing pays one config read
        for the whole table instead of one per row.

        ``honor_recent_failure=False`` skips the recorded-failure question for a
        caller that is not making a CHOICE — a resume returning a session to the
        harness it already has. A recorded failure describes one spawn attempt and
        only a successful spawn clears it, so a resume that honoured it would be
        refused for the whole failure window even after the operator fixed the
        cause, and the refusal is what prevents the spawn that would clear it. The
        two questions that describe the machine as it is now (serviceable,
        executable resolves) are always asked.
        """
        harness_id = descriptor.id
        unserviceable = _UNSERVICEABLE.get(harness_id)
        if unserviceable:
            return False, unserviceable
        if honor_recent_failure:
            probe_reason = self._probe_failure(harness_id)
            if probe_reason:
                return False, probe_reason
        reasons = validate_descriptor(descriptor)
        if reasons:
            # Unreachable for an operator descriptor (an invalid one is never
            # registered) and a bug for a bundled one, so it is reported rather
            # than swallowed: a bundled descriptor that stops validating must be
            # visible as itself, not as a missing binary.
            return False, "; ".join(reasons)
        _path, reason = resolve_executable(descriptor)
        if reason:
            return False, reason
        return True, ""

    def _probe_failure(self, harness_id: str) -> str:
        """The recorded spawn failure for ``harness_id``, if it has not expired."""
        with self._lock:
            entry = self._probe_failures.get(harness_id)
            if entry is None:
                return ""
            recorded_at, reason = entry
            if (time.monotonic() - recorded_at) >= _PROBE_FAILURE_TTL_SECS:
                del self._probe_failures[harness_id]
                return ""
            return reason

    def note_probe_failure(self, harness_id: str, reason: str) -> None:
        """Record that ``harness_id`` failed to start, so listings say so.

        Called by the spawn path when a harness dies during ACP initialize (an
        unauthenticated harness is the common case). Stored per harness, so one
        failure never touches another harness's availability (R6.4).
        """
        if not harness_id or not reason:
            return
        with self._lock:
            self._probe_failures[harness_id] = (time.monotonic(), reason)

    def clear_probe_failure(self, harness_id: str) -> None:
        """Forget a recorded failure — called after ``harness_id`` starts cleanly."""
        with self._lock:
            self._probe_failures.pop(harness_id, None)

    def require_available(
        self, harness_id: str, *, honor_recent_failure: bool = True
    ) -> HarnessDescriptor:
        """The descriptor for ``harness_id``, or a refusal naming the harness.

        The one entry point for a selection surface: it raises
        :class:`UnknownHarness` or :class:`HarnessUnavailable` rather than
        returning a substitute, because every surface's rule is
        refusal-over-fallback — silently serving a different harness than the one
        asked for is the failure this whole feature is meant to make impossible.

        ``honor_recent_failure=False`` is for a caller returning a session to the
        harness it is already bound to rather than choosing one; see
        :meth:`_availability`.
        """
        descriptor = self.get(harness_id)
        available, reason = self._availability(
            descriptor, honor_recent_failure=honor_recent_failure
        )
        if not available:
            raise HarnessUnavailable(harness_id, reason)
        return descriptor

    # ── Listings ──

    def list(self) -> tuple[HarnessListing, ...]:
        """Every registered harness, each marked available or unavailable.

        Bundled harnesses come first in declaration order (default first), then
        operator harnesses sorted by id, so a selection surface renders a stable
        list. Returned as a tuple — the design writes ``list[HarnessListing]``,
        and a tuple is the same thing to every consumer while keeping a caller
        from mutating the registry's view of the world.

        Invalid operator descriptors are NOT here; see :meth:`invalid`.
        """
        self._ensure_loaded()
        bundled_ids = [d.id for d in BUNDLED_DESCRIPTORS if d.id in self._descriptors]
        operator_ids = sorted(set(self._descriptors) - set(bundled_ids))
        rows: list[HarnessListing] = []
        for harness_id in [*bundled_ids, *operator_ids]:
            descriptor = self._descriptors[harness_id]
            available, reason = self._availability(descriptor)
            rows.append(
                HarnessListing(
                    id=harness_id,
                    display_name=descriptor.label,
                    available=available,
                    reason=reason,
                    bundled=harness_id in bundled_ids,
                )
            )
        return tuple(rows)

    def invalid(self) -> tuple[HarnessListing, ...]:
        """Operator descriptors that failed validation, with their reasons.

        Separate from :meth:`list` so a selection surface cannot reach one, while
        Settings can still render "this entry of yours is broken, here is why" —
        which is the whole value of recording the reason (R2.3, R6.2).
        """
        self._ensure_loaded()
        return tuple(
            HarnessListing(
                id=harness_id,
                display_name=harness_id,
                available=False,
                reason="; ".join(reasons),
                bundled=False,
                valid=False,
            )
            for harness_id, reasons in sorted(self._invalid.items())
        )


_REGISTRY = HarnessRegistry()


def registry() -> HarnessRegistry:
    """The process-wide registry.

    A function rather than a bare module global so a test can reach the same
    instance every consumer does, and so the singleton is constructed at import
    without touching configuration or the filesystem (R6.1).
    """
    return _REGISTRY
