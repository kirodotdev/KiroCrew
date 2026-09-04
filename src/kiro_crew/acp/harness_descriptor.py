"""Harness descriptor: the data record describing one ACP-speaking harness.

A harness is an executable that speaks ACP on stdio and drives an LLM with its
own authentication (kiro-cli, Codex, Claude Code, KAS, or an operator's own ACP
server). This module owns the DATA and the two pure operations over it —
validation and argv rendering — so that spawning a harness never needs a
per-harness code branch.

Three properties are load-bearing and every function here is written to keep
them:

- **Validation returns reasons, it does not raise.** An operator's descriptor
  arrives from configuration and may be arbitrary garbage; a malformed one must
  cost that harness its place in the selection surfaces, never the gateway's
  boot. So the failure channel is a ``list[str]`` of diagnosable reasons that a
  caller can record and show, and the parse entry point tolerates any input
  shape rather than trusting the config layer to have pre-validated it.
- **Rendering is total and shell-free.** ``render_argv`` returns a ``list[str]``
  built by token-wise substitution. Nothing here concatenates a command line,
  quotes an argument, or consults a shell, so an operator-supplied executable,
  agent name, or model id can never become shell syntax.
- **Capabilities default OFF.** A descriptor that never mentions a capability
  does not get it. The alternative fails open: a harness that has demonstrated
  nothing would inherit a feature (a sandbox waiver, session sharing) the
  operator never granted it. See docs/system-specs/modules/harness-parity.md.
- **Code and configuration are not the same authority.** Bundled descriptors are
  constructed in code and may name an ``adapter``; an operator's descriptor is
  parsed from ``config.json`` and cannot, because a config key that chose a
  Python entry point would let configuration select code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Collection, Iterable, Mapping

from kiro_crew.acp_backends import (  # noqa: F401 - re-exported for existing importers
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)

# ── Legacy ACP backend identifiers ──
# The ``acp_backend`` string a session/provider is keyed on while the harness
# seam is still being wired through. DEFINED in the top-level leaf
# ``kiro_crew.acp_backends`` (the single owner after upstream's #6593 registry
# refactor) and re-exported here. ``protocol_profile`` needs them to map a
# backend string to a wire profile, and ``acp.types`` imports ``harness_registry``
# which imports ``harness_adapters`` — so a ``protocol_profile`` -> ``acp.types``
# edge would close a module-scope cycle. ``acp_backends`` imports nothing from
# this package (it is a true leaf too), so re-exporting from it here is cycle-free
# and keeps ONE definition of the identifiers. ``acp.types`` re-exports the same
# names for compatibility, so existing ``from kiro_crew.acp.types import
# ACP_BACKEND_*`` call sites keep working.


# ── Capability vocabulary ──
# Each name is a feature the code currently assumes kiro-cli has. A capability
# is an opt-in claim: membership is an explicit decision per harness, never an
# inference from "this is not some other harness".

CAPABILITY_SESSION_SHARING = "session_sharing"
CAPABILITY_STEER = "steer"
CAPABILITY_INTERNAL_SANDBOX = "internal_sandbox"
CAPABILITY_ACP_RUNTIME_POOL = "acp_runtime_pool"
CAPABILITY_KIRO_IDENTITY_STORE = "kiro_identity_store"
CAPABILITY_MCP_TOOL_SEARCH = "mcp_tool_search"
CAPABILITY_REASONING_EFFORT = "reasoning_effort"

#: Every capability name a descriptor may mention, in declaration order.
CAPABILITY_NAMES: tuple[str, ...] = (
    CAPABILITY_SESSION_SHARING,
    CAPABILITY_STEER,
    CAPABILITY_INTERNAL_SANDBOX,
    CAPABILITY_ACP_RUNTIME_POOL,
    CAPABILITY_KIRO_IDENTITY_STORE,
    CAPABILITY_MCP_TOOL_SEARCH,
    CAPABILITY_REASONING_EFFORT,
)

# ── Model source ──

#: The harness enumerates its own models over ACP (the default).
MODEL_SOURCE_ACP_ADVERTISED = "acp_advertised"
#: The descriptor carries a fixed list, for a harness that cannot enumerate.
MODEL_SOURCE_STATIC = "static"
MODEL_SOURCES = frozenset({MODEL_SOURCE_ACP_ADVERTISED, MODEL_SOURCE_STATIC})

# ── Adapters (bundled, reviewed code only) ──
# A harness whose pre-spawn or post-initialize needs cannot be expressed as argv
# data names an adapter: a Python entry point that ships with Kiro Crew. The
# vocabulary is closed and lists only adapters that EXIST — naming one that does
# not is a descriptor that validates and then fails at spawn, which is the
# failure this module exists to move earlier.
#
# Deliberately absent from :data:`DESCRIPTOR_KEYS`: an operator descriptor that
# could name an adapter would be configuration selecting arbitrary Python, so
# harness-specific code ships only through review. See the registry module for
# what each adapter carries.
ADAPTER_KIRO = "kiro"
ADAPTER_KAS = "kas"
ADAPTER_CLAUDE = "claude"
#: The adapter every adapter-less descriptor resolves to — the generic ACP rule
#: (PATH/absolute resolution and pure template rendering, no harness-specific
#: code). It is named so the instance can be registered and referred to like the
#: bespoke ones, and it is in the vocabulary so a bundled descriptor MAY state it
#: explicitly; an operator descriptor still cannot name any adapter (``adapter``
#: is not in :data:`DESCRIPTOR_KEYS`) and reaches the generic rule by leaving the
#: field unset.
ADAPTER_GENERIC = "generic"
ADAPTERS = frozenset({ADAPTER_KIRO, ADAPTER_KAS, ADAPTER_CLAUDE, ADAPTER_GENERIC})

# ── MCP delivery ──

#: The harness loads MCP servers from its own configuration channel.
MCP_DELIVERY_FILE_FED = "file_fed"
#: The harness learns its MCP servers only from the ACP session/new request.
MCP_DELIVERY_WIRE_FED = "wire_fed"
MCP_DELIVERIES = frozenset({MCP_DELIVERY_FILE_FED, MCP_DELIVERY_WIRE_FED})
#: Wire-fed is the default for an omitted declaration: the ACP specification
#: requires every conformant agent to accept the stdio server list at
#: session/new, so it is the one channel a harness cannot have opted out of.
MCP_DELIVERY_DEFAULT = MCP_DELIVERY_WIRE_FED

# ── Argv placeholder vocabulary (closed) ──

PLACEHOLDER_EXECUTABLE = "{executable}"
PLACEHOLDER_AGENT = "{agent}"
PLACEHOLDER_MODEL = "{model}"
PLACEHOLDER_WORKDIR = "{workdir}"
#: The complete set. Anything else brace-wrapped in a template is a validation
#: failure rather than a literal, because silently passing an unknown
#: ``{...}`` token through to exec would hand the harness a meaningless
#: argument and produce a failure far from its cause.
ARGV_PLACEHOLDERS = frozenset(
    {
        PLACEHOLDER_EXECUTABLE,
        PLACEHOLDER_AGENT,
        PLACEHOLDER_MODEL,
        PLACEHOLDER_WORKDIR,
    }
)

# ── Identifier shape ──

#: Harness ids are lowercase kebab: they appear in config keys, API query
#: parameters, session records, and spawn arguments, so the charset is kept to
#: what is safe and unambiguous in all four.
HARNESS_ID_MAX_LEN = 32
_HARNESS_ID_RE = re.compile(r"[a-z0-9-]+")
#: Any brace-wrapped run in a template token. Matching the whole run (rather
#: than scanning for known placeholders) is what makes an unknown placeholder
#: detectable instead of surviving as a literal.
_BRACED_RE = re.compile(r"\{[^{}]*\}")

#: The mapping keys an operator descriptor may carry. Unknown keys fail
#: validation: a typo'd key would otherwise be silently ignored, leaving the
#: operator with a harness that quietly does not do what they configured.
#:
#: ``adapter`` is deliberately NOT here, and must never be added. A bundled
#: descriptor is constructed in code and may name one; an operator's descriptor
#: arrives from ``config.json`` and gets argv-template semantics only, because a
#: config key that selected a Python entry point would let configuration choose
#: code. ``test_harness_registry.py`` pins the literal key's rejection so
#: extending this set cannot open that path by accident.
DESCRIPTOR_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "display_name",
        "executable",
        "argv",
        "agent_args",
        "model_args",
        "capabilities",
        "model_source",
        "models",
        "mcp_delivery",
    }
)


@dataclass(frozen=True)
class CapabilitySet:
    """Per-harness feature flags, every one defaulting to disabled."""

    session_sharing: bool = False
    steer: bool = False
    internal_sandbox: bool = False
    acp_runtime_pool: bool = False
    kiro_identity_store: bool = False
    mcp_tool_search: bool = False
    reasoning_effort: bool = False

    def has(self, name: str) -> bool:
        """True when this set claims ``name``.

        An unknown name is a programming error (a capability gate naming a flag
        that does not exist), not operator data, so it raises rather than
        answering False — answering False would silently disable a real feature
        after a rename.
        """
        if name not in CAPABILITY_NAMES:
            raise ValueError(f"unknown harness capability {name!r}")
        return bool(getattr(self, name))

    def as_dict(self) -> dict[str, bool]:
        """The flags as a plain mapping, for serialization and listings."""
        return {name: bool(getattr(self, name)) for name in CAPABILITY_NAMES}


@dataclass(frozen=True)
class HarnessDescriptor:
    """One harness described as data: command, conventions, capabilities.

    Immutable so a descriptor handed to a session at creation cannot be edited
    underneath it — a session's binding has to outlive changes to the
    registry's copy (a persisted default change must never retarget a live
    session). Sequence fields are tuples for the same reason.
    """

    id: str
    display_name: str = ""
    #: Absolute path or a PATH-resolvable name. Resolution and trust
    #: attestation happen at spawn time; this is only the rule.
    executable: str = ""
    #: Ordered token list: literals and placeholders, always rendered as argv.
    #: The first token must be ``{executable}`` so the attested executable is the
    #: one that execs (see :func:`validate_descriptor`).
    argv: tuple[str, ...] = ()
    #: Optional convention block, emitted only when an agent is selected.
    agent_args: tuple[str, ...] = ()
    #: Optional convention block, emitted only when a model is selected.
    model_args: tuple[str, ...] = ()
    capabilities: CapabilitySet = field(default_factory=CapabilitySet)
    model_source: str = MODEL_SOURCE_ACP_ADVERTISED
    #: Consulted only when ``model_source`` is ``static``.
    models: tuple[str, ...] = ()
    mcp_delivery: str = MCP_DELIVERY_DEFAULT
    #: Bundled-only: the name of a reviewed Python entry point carrying this
    #: harness's pre-spawn / post-initialize steps (see :data:`ADAPTERS`). Empty
    #: for every operator descriptor, and :func:`descriptor_from_mapping` has no
    #: way to set it — configuration must never be able to select code.
    adapter: str = ""

    @property
    def label(self) -> str:
        """Human-facing name, falling back to the id when none was given."""
        return self.display_name or self.id


def _reason_prefix(harness_id: str) -> str:
    """Locate a reason on its harness even when the id itself is the problem."""
    return f"harness {harness_id!r}: " if harness_id else "harness: "


def _placeholder_reasons(prefix: str, block: str, tokens: Iterable[Any]) -> list[str]:
    """Reasons for every bad token in ``tokens``: non-string, unknown placeholder,
    unbalanced brace, or a convention placeholder used outside its own block.

    ``{agent}`` and ``{model}`` are CONVENTION placeholders: ``render_argv`` emits
    the ``agent_args`` block only when an agent is selected and the ``model_args``
    block only when a model is pinned, so those placeholders are only meaningful
    where that gating applies. In the ungated ``argv`` block — or in each other's
    block — they render to the empty string whenever the value is absent, execing
    a silent empty argument (``--model=`` or a bare ``""``). Rejecting them at
    validation turns that footgun into a registration-time reason instead of a
    spawn that half-works. ``{executable}`` and ``{workdir}`` carry no gating and
    stay legal in every block.
    """
    # The convention placeholder each gated block owns; every other block that is
    # not that block's home rejects it.
    _CONVENTION_HOME = {
        PLACEHOLDER_AGENT: "agent_args",
        PLACEHOLDER_MODEL: "model_args",
    }
    reasons: list[str] = []
    for token in tokens:
        if not isinstance(token, str):
            reasons.append(f"{prefix}{block} token {token!r} is not a string")
            continue
        residue = token
        for match in _BRACED_RE.finditer(token):
            placeholder = match.group(0)
            if placeholder not in ARGV_PLACEHOLDERS:
                allowed = ", ".join(sorted(ARGV_PLACEHOLDERS))
                reasons.append(
                    f"{prefix}{block} token {token!r} uses unknown placeholder "
                    f"{placeholder} (allowed: {allowed})"
                )
            elif placeholder in _CONVENTION_HOME and _CONVENTION_HOME[placeholder] != block:
                reasons.append(
                    f"{prefix}{block} token {token!r} uses {placeholder}, which is "
                    f"only meaningful in {_CONVENTION_HOME[placeholder]} (that block "
                    f"is emitted only when the value is present; elsewhere "
                    f"{placeholder} renders to an empty argument)"
                )
            residue = residue.replace(placeholder, "", 1)
        # A leftover brace means the token is neither a placeholder nor a plain
        # literal — most often a typo like "--dir={workdir" whose rendered form
        # would reach exec unsubstituted.
        if "{" in residue or "}" in residue:
            reasons.append(f"{prefix}{block} token {token!r} has an unbalanced brace")
    return reasons


def _sequence_reasons(prefix: str, block: str, value: Any) -> list[str]:
    """Reason when ``value`` is not a sequence of tokens.

    A bare string is the case that matters: ``argv="my-tool acp"`` on a
    code-built descriptor is iterable, so every check downstream happily
    iterates it ONE CHARACTER AT A TIME and the descriptor validates. The argv
    it renders is ``["m", "y", "-", …]``, which fails at exec with an unreadable
    error far from the mistake. :func:`descriptor_from_mapping` already refuses
    the shape for operator config; this is the same refusal for a descriptor
    built in code.
    """
    if isinstance(value, (tuple, list)):
        return []
    kind = "a string" if isinstance(value, str) else f"{type(value).__name__}"
    return [f"{prefix}{block} must be a sequence of tokens, not {kind}"]


def validate_descriptor(
    descriptor: HarnessDescriptor,
    *,
    taken_ids: Collection[str] = (),
) -> list[str]:
    """Return every shape problem with ``descriptor``; empty means valid.

    ``taken_ids`` are ids already registered, so the caller gets uniqueness
    enforced with the same diagnosable-reason channel as everything else
    instead of having to compare ids itself.
    """
    prefix = _reason_prefix(descriptor.id)
    reasons: list[str] = []

    if not isinstance(descriptor.id, str) or not descriptor.id:
        reasons.append(f"{prefix}identifier is empty")
    elif not _HARNESS_ID_RE.fullmatch(descriptor.id):
        reasons.append(f"{prefix}identifier must use only lowercase letters, digits, and hyphens")
    elif len(descriptor.id) > HARNESS_ID_MAX_LEN:
        reasons.append(f"{prefix}identifier is longer than {HARNESS_ID_MAX_LEN} characters")
    elif descriptor.id in taken_ids:
        reasons.append(f"{prefix}identifier is already registered")

    if not isinstance(descriptor.executable, str) or not descriptor.executable:
        reasons.append(f"{prefix}executable is empty")

    # Shape before content: a bare-string argv passes every per-token check by
    # being iterated character-wise, so the sequence check has to come first and
    # the token checks have to be skipped when it fails.
    for block, tokens in (
        ("argv", descriptor.argv),
        ("agent_args", descriptor.agent_args),
        ("model_args", descriptor.model_args),
    ):
        shape = _sequence_reasons(prefix, block, tokens)
        if shape:
            reasons += shape
            continue
        if block == "argv" and not tokens:
            # Without at least one token there is no program to exec; the harness
            # would fail at spawn with an empty argv rather than at registration.
            reasons.append(f"{prefix}argv template is empty")
        elif block == "argv" and tokens[0] != PLACEHOLDER_EXECUTABLE:
            # argv[0] IS the program, and ``executable`` is the field that gets
            # resolved and trust-attested at spawn. A template whose first token
            # is a literal therefore execs bytes nobody checked: a bare name is
            # resolved by exec through PATH at spawn time, so the file that was
            # attested and the file that runs need not be the same one. Requiring
            # the placeholder is what makes the attestation load-bearing rather
            # than decorative (the spawn path re-checks the rendered argv, for
            # the adapters that build one without a template).
            reasons.append(
                f"{prefix}argv template must start with {PLACEHOLDER_EXECUTABLE} "
                f"so the executable that is trust-attested is the one that runs, "
                f"not {tokens[0]!r}"
            )
        reasons += _placeholder_reasons(prefix, block, tokens)

    if descriptor.model_source not in MODEL_SOURCES:
        allowed = ", ".join(sorted(MODEL_SOURCES))
        reasons.append(f"{prefix}model_source {descriptor.model_source!r} is not one of: {allowed}")
    elif descriptor.model_source == MODEL_SOURCE_STATIC and not descriptor.models:
        # Rejected rather than accepted-and-listed-unavailable: ``static`` is the
        # declaration "I cannot enumerate my models over ACP, here they are
        # instead", so an empty list leaves the composer with no model to offer
        # and no way to obtain one. The harness could never serve a session, and
        # a validation reason names the actual mistake at registration time
        # instead of surfacing as an empty dropdown the operator has to explain.
        reasons.append(f"{prefix}model_source is 'static' but no models are declared")

    if descriptor.mcp_delivery not in MCP_DELIVERIES:
        allowed = ", ".join(sorted(MCP_DELIVERIES))
        reasons.append(f"{prefix}mcp_delivery {descriptor.mcp_delivery!r} is not one of: {allowed}")

    shape = _sequence_reasons(prefix, "models", descriptor.models)
    if shape:
        reasons += shape
    else:
        for model in descriptor.models:
            if not isinstance(model, str) or not model:
                reasons.append(f"{prefix}models entry {model!r} is not a non-empty string")

    if not isinstance(descriptor.capabilities, CapabilitySet):
        reasons.append(f"{prefix}capabilities is not a capability set")

    if descriptor.adapter and descriptor.adapter not in ADAPTERS:
        allowed = ", ".join(sorted(ADAPTERS))
        reasons.append(f"{prefix}adapter {descriptor.adapter!r} is not one of: {allowed}")

    return reasons


#: Capabilities an OPERATOR DESCRIPTOR may not claim. Each of these is honoured
#: by code written for a specific bundled harness, so a config grant does not
#: light the feature up — it points trusted machinery at a process that never
#: earned it:
#:
#: * ``internal_sandbox`` waives Kiro Crew's own OS sandbox in favour of the
#:   harness's internal one (kiro-cli's). Granted to a binary with no internal
#:   sandbox, the agent process runs UNCONFINED — the one fail-open in the
#:   capability set, and a security hole rather than a malfunction.
#: * ``acp_runtime_pool`` routes the session onto ``AcpRuntime``, which speaks
#:   kiro-cli's wire dialect (date-string protocolVersion, ``/effort``); a
#:   foreign harness there fails its handshake or, worse, half-works.
#: * ``session_sharing`` multiplexes one process across sessions and subagents
#:   through machinery built for kiro-cli's demux; a foreign process is reused
#:   across trust contexts it never declared it could separate.
#: * ``kiro_identity_store`` retires the process when KIRO-CLI's login changes;
#:   an operator harness authenticates itself, so the sweep would kill sessions
#:   on an identity store their harness never reads.
#:
#: Bundled descriptors construct :class:`CapabilitySet` directly in
#: ``harness_registry.py`` and never pass through this parser, so the grants
#: stay expressible where the honouring code actually exists. Rejected per key
#: with its own reason (not silently dropped): a dropped flag would read as
#: "Kiro Crew ignored my capability" with nothing to diagnose, and the whole
#: entry failing closed matches every other validation error's posture.
_CONFIG_UNGRANTABLE_CAPABILITIES: dict[str, str] = {
    CAPABILITY_INTERNAL_SANDBOX: (
        "it waives Kiro Crew's own sandbox in favour of a harness-internal one "
        "only kiro-cli has — granted from config it leaves the process unconfined"
    ),
    CAPABILITY_ACP_RUNTIME_POOL: (
        "the shared AcpRuntime speaks kiro-cli's wire dialect; a config-declared "
        "harness runs through the generic per-session client instead"
    ),
    CAPABILITY_SESSION_SHARING: (
        "session multiplexing is built for kiro-cli's demux; a config-declared "
        "harness gets one process per session"
    ),
    CAPABILITY_KIRO_IDENTITY_STORE: (
        "it ties the process's lifetime to kiro-cli's login; a config-declared "
        "harness authenticates itself"
    ),
}


def _capabilities_from_mapping(prefix: str, raw: Any) -> tuple[CapabilitySet, list[str]]:
    """Parse a capability mapping, defaulting every unmentioned flag to off."""
    if raw is None:
        return CapabilitySet(), []
    if not isinstance(raw, Mapping):
        return CapabilitySet(), [f"{prefix}capabilities must be an object"]
    reasons: list[str] = []
    flags: dict[str, bool] = {}
    for key, value in raw.items():
        if key not in CAPABILITY_NAMES:
            allowed = ", ".join(CAPABILITY_NAMES)
            reasons.append(f"{prefix}unknown capability {key!r} (allowed: {allowed})")
            continue
        # Strict bool: a truthy string such as "false" would otherwise ENABLE
        # the flag, which is the wrong direction to guess in.
        if not isinstance(value, bool):
            reasons.append(f"{prefix}capability {key!r} must be true or false")
            continue
        # A code-only capability is refused even as an explicit ``false``: the
        # key's presence in config is a claim about machinery this descriptor
        # can never bind, and accepting the spelling at all invites flipping it.
        if key in _CONFIG_UNGRANTABLE_CAPABILITIES:
            why = _CONFIG_UNGRANTABLE_CAPABILITIES[key]
            reasons.append(
                f"{prefix}capability {key!r} cannot be granted from configuration — {why}"
            )
            continue
        flags[key] = value
    if reasons:
        return CapabilitySet(), reasons
    return CapabilitySet(**flags), []


def _string_tuple(prefix: str, key: str, raw: Any) -> tuple[tuple[str, ...], list[str]]:
    """Coerce a JSON array of strings to a tuple, reporting the shape instead
    of guessing.

    A bare string is rejected rather than wrapped: ``"argv": "my-tool acp"``
    reads like a command line, and accepting it would imply the shell splitting
    that this module exists to avoid.
    """
    if raw is None:
        return (), []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return (), [f"{prefix}{key} must be an array of strings"]
    values: list[str] = []
    reasons: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            reasons.append(f"{prefix}{key} entry {item!r} is not a string")
            continue
        values.append(item)
    return tuple(values), reasons


def _optional_string(prefix: str, key: str, raw: Any, default: str) -> tuple[str, list[str]]:
    """Read an optional string field, keeping ``default`` when it is absent."""
    if raw is None:
        return default, []
    if not isinstance(raw, str):
        return default, [f"{prefix}{key} must be a string"]
    return raw, []


def descriptor_from_mapping(
    raw: Any,
    *,
    harness_id: str = "",
    taken_ids: Collection[str] = (),
) -> tuple[HarnessDescriptor | None, list[str]]:
    """Parse and validate an operator descriptor.

    ``harness_id`` is the id the descriptor was filed under (the
    ``harnesses.json`` map); a descriptor may also carry it as an ``id``
    field, and the two must agree. Returns ``(None, reasons)`` when anything is
    wrong and never raises, so a registry can record the reasons and keep
    serving every other harness.
    """
    prefix = _reason_prefix(harness_id)
    if not isinstance(raw, Mapping):
        return None, [f"{prefix}descriptor must be an object"]

    reasons: list[str] = []
    unknown = sorted(str(key) for key in raw.keys() if key not in DESCRIPTOR_KEYS)
    if unknown:
        allowed = ", ".join(sorted(DESCRIPTOR_KEYS))
        reasons.append(
            f"{prefix}unknown field(s) {', '.join(repr(k) for k in unknown)} "
            f"(allowed: {allowed})"
        )

    declared_id, id_reasons = _optional_string(prefix, "id", raw.get("id"), harness_id)
    reasons += id_reasons
    if harness_id and declared_id and declared_id != harness_id:
        reasons.append(f"{prefix}declared id {declared_id!r} does not match its registry key")
        declared_id = harness_id
    if not declared_id:
        reasons.append(f"{prefix}identifier is empty")

    display_name, name_reasons = _optional_string(
        prefix, "display_name", raw.get("display_name"), ""
    )
    reasons += name_reasons
    executable, exe_reasons = _optional_string(prefix, "executable", raw.get("executable"), "")
    reasons += exe_reasons
    argv, argv_reasons = _string_tuple(prefix, "argv", raw.get("argv"))
    reasons += argv_reasons
    agent_args, agent_reasons = _string_tuple(prefix, "agent_args", raw.get("agent_args"))
    reasons += agent_reasons
    model_args, model_arg_reasons = _string_tuple(prefix, "model_args", raw.get("model_args"))
    reasons += model_arg_reasons
    models, models_reasons = _string_tuple(prefix, "models", raw.get("models"))
    reasons += models_reasons
    capabilities, cap_reasons = _capabilities_from_mapping(prefix, raw.get("capabilities"))
    reasons += cap_reasons
    model_source, source_reasons = _optional_string(
        prefix, "model_source", raw.get("model_source"), MODEL_SOURCE_ACP_ADVERTISED
    )
    reasons += source_reasons
    mcp_delivery, delivery_reasons = _optional_string(
        prefix, "mcp_delivery", raw.get("mcp_delivery"), MCP_DELIVERY_DEFAULT
    )
    reasons += delivery_reasons

    descriptor = HarnessDescriptor(
        id=declared_id,
        display_name=display_name,
        executable=executable,
        argv=argv,
        agent_args=agent_args,
        model_args=model_args,
        capabilities=capabilities,
        model_source=model_source,
        models=models,
        mcp_delivery=mcp_delivery,
    )
    reasons += validate_descriptor(descriptor, taken_ids=taken_ids)
    if reasons:
        # Deduplicate while preserving order: a field can be reported by both
        # the parse step and the shape check, and a listing should show the
        # reason once.
        return None, list(dict.fromkeys(reasons))
    return descriptor, []


def _substitute(token: str, values: Mapping[str, str]) -> str:
    """Replace known placeholders in ``token`` in a SINGLE pass.

    Single-pass matters: a model id or agent name that happens to contain
    ``{workdir}`` must reach exec as those literal bytes, not as the working
    directory. Unknown placeholders are left untouched — validation is where
    they are rejected, and rendering stays total so a caller can never be
    handed an exception mid-spawn.
    """
    return _BRACED_RE.sub(lambda m: values.get(m.group(0), m.group(0)), token)


def render_argv(
    descriptor: HarnessDescriptor,
    *,
    executable: str = "",
    agent: str = "",
    model: str = "",
    workdir: str | os.PathLike[str] = "",
) -> list[str]:
    """Render ``descriptor``'s template into a concrete argv list.

    ``executable`` overrides ``descriptor.executable`` (spawn resolves a PATH
    name to an absolute, attested path before rendering). The optional
    convention blocks are emitted only when their value is non-empty, so a
    harness with no model selected gets no dangling ``--model`` flag rather
    than an empty argument, and a descriptor that declares no block for a
    convention never receives a substituted default from another harness.

    The result is a plain ``list[str]`` for ``subprocess`` with no shell: every
    value lands as exactly one argv element regardless of the spaces, quotes,
    or metacharacters it contains.
    """
    values = {
        PLACEHOLDER_EXECUTABLE: executable or descriptor.executable,
        PLACEHOLDER_AGENT: agent,
        PLACEHOLDER_MODEL: model,
        PLACEHOLDER_WORKDIR: os.fspath(workdir) if workdir else "",
    }
    rendered = [_substitute(token, values) for token in descriptor.argv]
    if agent:
        rendered += [_substitute(token, values) for token in descriptor.agent_args]
    if model:
        rendered += [_substitute(token, values) for token in descriptor.model_args]
    return rendered


def capability_names() -> tuple[str, ...]:
    """The capability vocabulary, derived from the dataclass declaration.

    Kept as a function so a flag added to :class:`CapabilitySet` without a
    matching entry in :data:`CAPABILITY_NAMES` is caught by the pin test rather
    than becoming a flag nothing can read.
    """
    return tuple(f.name for f in fields(CapabilitySet))
