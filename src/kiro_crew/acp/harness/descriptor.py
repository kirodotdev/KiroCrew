"""Harness descriptor: the data record describing one operator-defined ACP harness.

A harness is an executable that speaks ACP on stdio and drives an LLM with its
own authentication (kiro-cli, Codex, Claude Code, KAS, or an operator's own ACP
server). The BUNDLED harnesses are now real classes upstream
(``kiro_crew.acp.harness.base.HarnessAdapter`` and its subclasses); a
*descriptor* is the operator-authored form only. It carries no way to name a
Python entry point — an operator writes ``harnesses.json`` and gets pure
argv-template semantics, because a config key that selected code would let
configuration choose code.

This module owns the DATA and the two pure operations over it — validation and
argv rendering — so that spawning a descriptor-defined harness never needs a
per-harness code branch. It performs NO registration wiring; that is Wave 2.

Four properties are load-bearing and every function here is written to keep
them:

- **Validation returns reasons, it does not raise.** An operator's descriptor
  arrives from configuration and may be arbitrary garbage; a malformed one must
  cost that harness its place in the selection surfaces, never the gateway's
  boot. So the failure channel is a ``list[str]`` of diagnosable reasons that a
  caller can record and show, and the loader tolerates any input shape (missing
  file, malformed JSON, non-dict entries) rather than trusting the config layer
  to have pre-validated it. A malformed entry costs its row, never boot.
- **Rendering is total and shell-free.** ``render_argv`` returns a ``list[str]``
  built by token-wise substitution. Nothing here concatenates a command line,
  quotes an argument, or consults a shell, so an operator-supplied executable,
  agent name, or model id can never become shell syntax.
- **No capability claims.** A descriptor cannot grant its harness a session-path
  capability. Every ``ACP_BACKENDS_*`` membership set in
  ``kiro_crew.agent_sdk.backends`` is a frozen, code-reviewed literal, and a
  registered id is a member of none of them, so a config-authored host carries
  the DEFAULT posture at every gate (no ``session/new`` MCP array, no
  harness-owned resume, no model switch over ``set_config_option``, no
  advertised-list model resolution). Admitting a harness to a capability is an
  edit to the named set, not a line in ``harnesses.json`` (harness-parity H6/H7).
  A ``capabilities`` key is therefore refused as unknown.
- **Selectability is opt-in through routing.** A descriptor is registered as
  *known* whenever it validates, but it is only *selectable* — offered as a
  session backend — when it declares how permission routing is delivered (see
  ``routing`` below). Absent or unrecognized routing means known-but-unselectable:
  visible in Settings with a reason, never silently servable.

This module defines no ``ACP_BACKENDS_*`` constant and imports none, per
``scripts/check_harness_parity.py``: the vocabulary home is
``kiro_crew.agent_sdk.backends``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Mapping

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# ── Model source ──

#: The harness enumerates its own models over ACP (the default).
MODEL_SOURCE_ACP_ADVERTISED = "acp_advertised"
#: The descriptor carries a fixed list, for a harness that cannot enumerate.
MODEL_SOURCE_STATIC = "static"
MODEL_SOURCES = frozenset({MODEL_SOURCE_ACP_ADVERTISED, MODEL_SOURCE_STATIC})

# ── Routing (selectability gate) ──
# How a session's permission decision reaches the harness. A descriptor is
# registered as KNOWN whenever it validates, but SELECTABLE only when routing is
# one of these two verified forms; anything else (absent, or an unrecognized
# string) registers the harness as known-but-unselectable — a visible row in
# Settings with a reason, never a servable backend. This mirrors the
# ImportSource precedent: a descriptor never names code, so the only routings it
# may claim are the two the generic mask path can honour.

#: The permission decision travels as an agent-spec (``--agent``-style) selection.
ROUTING_AGENT_SPEC = "agent_spec"
#: The permission decision travels over ``session/set_config_option``; requires a
#: ``permission_config`` object naming the option and the value to set.
ROUTING_SESSION_CONFIG = "session_config"
ROUTINGS = frozenset({ROUTING_AGENT_SPEC, ROUTING_SESSION_CONFIG})

# ── MCP delivery ──
# How the harness receives its MCP server list: the delivery MECHANISM the
# spawn/session path keys on (``DescriptorHarness.session_mcp_servers``),
# expressed in the descriptor-safe vocabulary that maps onto upstream's harness
# ``session_mcp_servers`` behaviour. This is a harness-seam decision, not a
# session-path capability grant: it changes what the harness object does with the
# array, never which frozen ``ACP_BACKENDS_*`` set the client consults.

#: Passthrough: the harness loads MCP servers from its own agent-spec/config
#: channel and Crew sends the ``session/new`` array unchanged (the kiro-family
#: posture: ``session_mcp_servers`` returns ``requested`` untouched).
MCP_DELIVERY_AGENT_FILE = "agent_file"
#: Array with transport narrowing: the harness learns its servers only from the
#: ``session/new`` ``mcpServers`` array, and Crew drops any element whose
#: transport the harness did not advertise at ``initialize`` (the codex/claude
#: posture: ``drop_unadvertised_transports`` against advertised
#: ``mcpCapabilities``).
MCP_DELIVERY_SESSION_ARRAY = "session_array"
MCP_DELIVERIES = frozenset({MCP_DELIVERY_AGENT_FILE, MCP_DELIVERY_SESSION_ARRAY})
#: Agent-file passthrough is the default for an omitted declaration: it is the
#: posture that touches nothing, sending the caller's array through unchanged.
MCP_DELIVERY_DEFAULT = MCP_DELIVERY_AGENT_FILE

# ── Argv placeholder vocabulary (closed) ──

PLACEHOLDER_EXECUTABLE = "{executable}"
PLACEHOLDER_AGENT = "{agent}"
PLACEHOLDER_MODEL = "{model}"
PLACEHOLDER_WORKDIR = "{workdir}"
#: The complete set. Anything else brace-wrapped in a template is a validation
#: failure rather than a literal, because silently passing an unknown ``{...}``
#: token through to exec would hand the harness a meaningless argument and
#: produce a failure far from its cause.
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
#: Bounds on the operator file itself, so a malformed or oversized
#: ``harnesses.json`` costs a diagnosable whole-file invalid entry rather than
#: boot-time memory. A descriptor is a handful of short strings and a few
#: small lists; a real file of even a dozen descriptors is well under 64 KiB,
#: and no deployment names more than a few dozen hosts. Both caps are
#: generous over that and are refusals of a shape that is not a descriptor
#: file at all. The byte cap bounds everything retained from the file (every
#: string a descriptor keeps is a substring of it); the count cap bounds how
#: many registrations, harness objects and diagnostic rows one file can create.
HARNESSES_FILE_MAX_BYTES = 1 * 1024 * 1024
HARNESSES_MAX_DESCRIPTORS = 64
#: Any brace-wrapped run in a template token. Matching the whole run (rather
#: than scanning for known placeholders) is what makes an unknown placeholder
#: detectable instead of surviving as a literal.
_BRACED_RE = re.compile(r"\{[^{}]*\}")

#: The keys of a ``permission_config`` object (required when routing is
#: ``session_config``). Closed for the same reason as the top-level key set.
PERMISSION_CONFIG_KEYS: frozenset[str] = frozenset({"option", "value"})

#: The mapping keys an operator descriptor may carry. Unknown keys fail
#: validation: a typo'd key would otherwise be silently ignored, leaving the
#: operator with a harness that quietly does not do what they configured.
#:
#: There is deliberately NO ``adapter`` key. A descriptor arrives from
#: ``harnesses.json`` and gets argv-template semantics only; the bundled
#: harnesses are real classes upstream and never pass through this parser, so
#: configuration can never select a Python entry point.
DESCRIPTOR_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "display_name",
        "executable",
        "argv",
        "agent_args",
        "model_args",
        "model_source",
        "models",
        "mcp_delivery",
        "routing",
        "permission_config",
    }
)

#: The file the operator writes, beside ``config.json`` under the crew home.
OPERATOR_HARNESSES_LEAF = "harnesses.json"


@dataclass(frozen=True)
class PermissionConfig:
    """The config-option write that carries a ``session_config`` harness's
    permission decision: ``session/set_config_option(option, value)``."""

    option: str
    value: str


@dataclass(frozen=True)
class HarnessDescriptor:
    """One operator-defined harness described as data: command, conventions,
    routing.

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
    model_source: str = MODEL_SOURCE_ACP_ADVERTISED
    #: Consulted only when ``model_source`` is ``static``.
    models: tuple[str, ...] = ()
    mcp_delivery: str = MCP_DELIVERY_DEFAULT
    #: How the permission decision is delivered. Empty (or unrecognized) means
    #: the harness registers known-but-unselectable; see :data:`ROUTINGS`.
    routing: str = ""
    #: Required when ``routing`` is ``session_config``, forbidden otherwise.
    permission_config: PermissionConfig | None = None

    @property
    def label(self) -> str:
        """Human-facing name, falling back to the id when none was given."""
        return self.display_name or self.id

    @property
    def selectable(self) -> bool:
        """Whether this descriptor may be offered as a session backend.

        A VALID descriptor with a recognized routing is selectable; one whose
        routing is absent or unrecognized is known-but-unselectable. This is a
        pure predicate over already-validated data — the registry (Wave 2) is
        what refuses selection and surfaces the reason.
        """
        return self.routing in ROUTINGS


def _reason_prefix(position: int | None) -> str:
    """Locate a reason on its entry by FILE POSITION, never by the id text.

    The id is operator text and an invalid entry's id is exactly the field that
    may be wrong -- a pasted credential, a path, a typo -- and the listing that
    shows these reasons is not owner-gated. So a reason says which entry
    (``entry #3:``) and the row it hangs on carries the same synthetic identifier
    (:func:`synthetic_entry_id`); a caller with no position gets no prefix.
    """
    return f"entry #{position}: " if position is not None else ""


def synthetic_entry_id(position: int) -> str:
    """The identifier an INVALID ``harnesses.json`` entry is listed under.

    ``#<position>`` (1-based, file order) can never collide with a real id --
    :data:`_HARNESS_ID_RE` admits only lowercase letters, digits and hyphens --
    and never repeats the operator's key, which is the point: the key of an
    invalid entry is not shown anywhere a non-owner can read.
    """
    return f"#{position}"


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
    # Reasons name the offending token by POSITION, never by content. They are
    # rendered by ``GET /api/backends`` (deliberately not owner-gated: it is the
    # new-chat picker's listing), and an argv literal is operator-authored text
    # that may carry a credential (``--api-key=...``). Placeholder names are
    # vocabulary and safe to echo; the token itself is not.
    for index, token in enumerate(tokens):
        where = f"{prefix}{block}[{index}]"
        if not isinstance(token, str):
            reasons.append(f"{where} is not a string")
            continue
        residue = token
        for match in _BRACED_RE.finditer(token):
            placeholder = match.group(0)
            if placeholder not in ARGV_PLACEHOLDERS:
                # The braced text is operator-authored, not vocabulary: a
                # secret written as ``{sk-...}`` would otherwise reach the
                # unauthenticated listing. Position only.
                allowed = ", ".join(sorted(ARGV_PLACEHOLDERS))
                reasons.append(f"{where} uses an unknown placeholder (allowed: {allowed})")
            elif placeholder in _CONVENTION_HOME and _CONVENTION_HOME[placeholder] != block:
                reasons.append(
                    f"{where} uses {placeholder}, which is "
                    f"only meaningful in {_CONVENTION_HOME[placeholder]} (that block "
                    f"is emitted only when the value is present; elsewhere "
                    f"{placeholder} renders to an empty argument)"
                )
            residue = residue.replace(placeholder, "", 1)
        # A leftover brace means the token is neither a placeholder nor a plain
        # literal — most often a typo like "--dir={workdir" whose rendered form
        # would reach exec unsubstituted.
        if "{" in residue or "}" in residue:
            reasons.append(f"{where} has an unbalanced brace")
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


def _delivers_agent(agent_args: Any) -> bool:
    """True when ``agent_args`` is a token sequence with a ``{agent}`` token.

    Only the substituted placeholder can carry the selected agent to the
    binary; a block of fixed flags with no ``{agent}`` renders the same argv
    whatever agent the operator picked. Shape errors (a bare string, a non-string
    token) are reported by the block checks and read as "does not deliver" here
    rather than raising over them.
    """
    if not isinstance(agent_args, (tuple, list)):
        return False
    return any(isinstance(tok, str) and PLACEHOLDER_AGENT in tok for tok in agent_args)


def validate_descriptor(
    descriptor: HarnessDescriptor,
    *,
    taken_ids: Collection[str] = (),
    position: int | None = None,
) -> list[str]:
    """Return every shape problem with ``descriptor``; empty means valid.

    ``taken_ids`` are ids already registered, so the caller gets uniqueness
    enforced with the same diagnosable-reason channel as everything else
    instead of having to compare ids itself. ``position`` is the entry's 1-based
    place in ``harnesses.json``; when given, every reason is prefixed with it
    (never with the id, which is operator text).

    A descriptor that validates with no recognized ``routing`` is VALID — it is
    known-but-unselectable, not malformed. Only an internally inconsistent
    routing (session_config without permission_config, or permission_config on a
    non-session_config routing) is a reason here.
    """
    prefix = _reason_prefix(position)
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
            # than decorative.
            reasons.append(
                f"{prefix}argv[0] must be {PLACEHOLDER_EXECUTABLE} "
                f"so the executable that is trust-attested is the one that runs"
            )
        reasons += _placeholder_reasons(prefix, block, tokens)

    if descriptor.model_source not in MODEL_SOURCES:
        allowed = ", ".join(sorted(MODEL_SOURCES))
        reasons.append(f"{prefix}model_source is not one of: {allowed}")
    elif descriptor.model_source == MODEL_SOURCE_STATIC and not descriptor.models:
        # Rejected rather than accepted-and-listed-unavailable: ``static`` is the
        # declaration "I cannot enumerate my models over ACP, here they are
        # instead", so an empty list leaves the composer with no model to offer
        # and no way to obtain one.
        reasons.append(f"{prefix}model_source is 'static' but no models are declared")

    if descriptor.mcp_delivery not in MCP_DELIVERIES:
        allowed = ", ".join(sorted(MCP_DELIVERIES))
        reasons.append(f"{prefix}mcp_delivery is not one of: {allowed}")

    # Routing: an empty routing is a valid known-but-unselectable descriptor, so
    # it is NOT a reason. A non-empty routing must be recognized, and the
    # permission_config coupling must hold in both directions.
    if descriptor.routing and descriptor.routing not in ROUTINGS:
        allowed = ", ".join(sorted(ROUTINGS))
        reasons.append(
            f"{prefix}routing is not one of: {allowed} "
            f"(leave routing empty to register the descriptor as "
            f"known-but-unselectable; an unrecognized value is rejected so a "
            f"misspelled routing cannot silently disable a backend)"
        )
    elif descriptor.routing == ROUTING_SESSION_CONFIG and descriptor.permission_config is None:
        reasons.append(
            f"{prefix}routing is 'session_config' but no permission_config "
            f"{{option, value}} is declared"
        )
    elif descriptor.routing == ROUTING_AGENT_SPEC and not _delivers_agent(descriptor.agent_args):
        # The same coupling as session_config/permission_config, in the other
        # direction: agent_spec routing says the permission decision travels
        # as an agent selection on the command line, and only ``agent_args``
        # can carry one. A descriptor that claims the routing but has no
        # ``{agent}`` token to deliver it would register as SELECTABLE and then
        # spawn the binary with no agent selected -- the default agent, with
        # nothing for the later activation check to confirm. Refuse it here so
        # the claim and the argv cannot disagree.
        reasons.append(
            f"{prefix}routing is 'agent_spec' but agent_args does not carry "
            f"{PLACEHOLDER_AGENT} (the routing says the agent selection travels "
            f"on the command line, and agent_args is the only block that can "
            f"deliver it)"
        )
    if descriptor.permission_config is not None:
        if descriptor.routing != ROUTING_SESSION_CONFIG:
            reasons.append(
                f"{prefix}permission_config is only meaningful when routing is " f"'session_config'"
            )
        else:
            pc = descriptor.permission_config
            if not isinstance(pc, PermissionConfig):
                reasons.append(f"{prefix}permission_config is not a permission config")
            else:
                if not isinstance(pc.option, str) or not pc.option:
                    reasons.append(f"{prefix}permission_config.option is empty")
                if not isinstance(pc.value, str) or not pc.value:
                    reasons.append(f"{prefix}permission_config.value is empty")

    shape = _sequence_reasons(prefix, "models", descriptor.models)
    if shape:
        reasons += shape
    else:
        for index, model in enumerate(descriptor.models):
            if not isinstance(model, str) or not model:
                reasons.append(f"{prefix}models[{index}] is not a non-empty string")

    return reasons


def _permission_config_from_mapping(
    prefix: str, raw: Any
) -> tuple[PermissionConfig | None, list[str]]:
    """Parse an optional ``permission_config`` object.

    Absent is ``(None, [])`` — the coupling with ``routing`` is enforced in
    :func:`validate_descriptor`, so this only reports the object's own shape.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, [f"{prefix}permission_config must be an object"]
    reasons: list[str] = []
    unknown = sorted(str(key) for key in raw.keys() if key not in PERMISSION_CONFIG_KEYS)
    if unknown:
        allowed = ", ".join(sorted(PERMISSION_CONFIG_KEYS))
        # Count, never names: a key is operator text (a credential pasted as a
        # key would otherwise reach the non-owner-gated listing).
        reasons.append(
            f"{prefix}permission_config has {len(unknown)} unknown field(s) (allowed: {allowed})"
        )
    option = raw.get("option")
    value = raw.get("value")
    if not isinstance(option, str) or not option:
        reasons.append(f"{prefix}permission_config.option must be a non-empty string")
        option = ""
    if not isinstance(value, str) or not value:
        reasons.append(f"{prefix}permission_config.value must be a non-empty string")
        value = ""
    if reasons:
        return None, reasons
    return PermissionConfig(option=option, value=value), []


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
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            # By position, not content: see ``_placeholder_reasons``.
            reasons.append(f"{prefix}{key}[{index}] is not a string")
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
    position: int | None = None,
) -> tuple[HarnessDescriptor | None, list[str]]:
    """Parse and validate an operator descriptor.

    ``harness_id`` is the id the descriptor was filed under (the
    ``harnesses.json`` map); a descriptor may also carry it as an ``id``
    field, and the two must agree. ``position`` is the entry's 1-based place in
    the file and is the only thing a reason uses to locate itself: the id is
    operator text, and an invalid entry's id may be the very thing that is wrong.
    Returns ``(None, reasons)`` when anything is wrong and never raises, so a
    registry can record the reasons and keep serving every other harness.
    """
    prefix = _reason_prefix(position)
    if not isinstance(raw, Mapping):
        return None, [f"{prefix}descriptor must be an object"]

    reasons: list[str] = []
    unknown = sorted(str(key) for key in raw.keys() if key not in DESCRIPTOR_KEYS)
    if unknown:
        allowed = ", ".join(sorted(DESCRIPTOR_KEYS))
        # Count, never names, for the same reason as every other reason here:
        # a key is operator text and the listing is not owner-gated.
        reasons.append(f"{prefix}{len(unknown)} unknown field(s) (allowed: {allowed})")

    declared_id, id_reasons = _optional_string(prefix, "id", raw.get("id"), harness_id)
    reasons += id_reasons
    if harness_id and declared_id and declared_id != harness_id:
        reasons.append(f"{prefix}declared id does not match its registry key")
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
    model_source, source_reasons = _optional_string(
        prefix, "model_source", raw.get("model_source"), MODEL_SOURCE_ACP_ADVERTISED
    )
    reasons += source_reasons
    mcp_delivery, delivery_reasons = _optional_string(
        prefix, "mcp_delivery", raw.get("mcp_delivery"), MCP_DELIVERY_DEFAULT
    )
    reasons += delivery_reasons
    routing, routing_reasons = _optional_string(prefix, "routing", raw.get("routing"), "")
    reasons += routing_reasons
    permission_config, pc_reasons = _permission_config_from_mapping(
        prefix, raw.get("permission_config")
    )
    reasons += pc_reasons

    descriptor = HarnessDescriptor(
        id=declared_id,
        display_name=display_name,
        executable=executable,
        argv=argv,
        agent_args=agent_args,
        model_args=model_args,
        model_source=model_source,
        models=models,
        mcp_delivery=mcp_delivery,
        routing=routing,
        permission_config=permission_config,
    )
    reasons += validate_descriptor(descriptor, taken_ids=taken_ids, position=position)
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


def operator_harnesses_path() -> "os.PathLike[str]":
    """The path to ``harnesses.json``, beside ``config.json`` under the crew home.

    Resolved through :func:`kiro_crew.config.paths.config_dir` — the same
    directory ``config.json`` resolves under — so it honours ``KIROCREW_HOME``
    and the test-pinned data home exactly as the config file does.
    """
    return config_dir() / OPERATOR_HARNESSES_LEAF


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses a repeated key instead of keeping the last.

    ``json.loads`` alone applies last-wins, so a file naming the same harness id
    twice would silently serve only the second body, and a descriptor repeating
    ``executable`` would silently take the later value. Neither is a state the
    operator wrote on purpose, and the id-uniqueness check downstream cannot see
    a collision the decoder already collapsed. Applies at every nesting level:
    a duplicate inside one descriptor is as much "two files in one" as a
    duplicate id.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            # The key is operator text (an id, a field name -- or something pasted
            # by mistake) and this message reaches the un-gated listing: say that a
            # key repeats, never which.
            raise ValueError("a key is repeated; every id and every field must appear once")
        seen[key] = value
    return seen


def load_operator_descriptors(
    *,
    path: "os.PathLike[str] | str | None" = None,
) -> tuple[tuple[HarnessDescriptor, ...], tuple[tuple[str, list[str]], ...]]:
    """Read ``harnesses.json`` and parse every entry.

    Returns ``(valid_descriptors, invalid_entries)`` where each invalid entry is
    ``(entry_id, reasons)`` and ``entry_id`` is the SYNTHETIC position id
    (:func:`synthetic_entry_id`, ``"#3"``) or ``""`` for a whole-file problem --
    never the operator's key. This function NEVER raises and NEVER wires
    anything into a registry — it is pure parse. Tolerance is total:

    - a MISSING file is an empty result (no harnesses configured is normal);
    - MALFORMED JSON is a single whole-file invalid entry (id ``""``) with the
      decoder's reason, so a typo in the file costs the file, not the boot;
    - a top-level value that is not an object is the same whole-file invalid
      entry;
    - a NON-DICT entry under an id is that id's invalid entry with a reason,
      never an exception;
    - ids are taken in file order, and a later duplicate id is rejected with a
      uniqueness reason (``descriptor_from_mapping`` sees the already-taken ids).
    """
    if path is None:
        path = operator_harnesses_path()
    text: str
    try:
        # The file is an execution grant sealed no-follow at spawn; the seal covers
        # the referent, so an alias at the NAME (a link, a second hardlink) is what
        # a forged grant would arrive through. Refused here, at the consumer, by
        # name and then of the very inode read -- nothing served on refusal.
        from kiro_crew.sandbox import SandboxCeilingUnsealable, require_unaliased_grant_file

        harm = "name the executable the gateway spawns as a backend"
        require_unaliased_grant_file(os.fspath(path), harm=harm)
        with open(path, "r", encoding="utf-8") as handle:
            require_unaliased_grant_file(os.fspath(path), harm=harm, fd=handle.fileno())
            # Bounded read: one byte past the cap is enough to know the file is
            # over it, without holding an oversized body. An operator file of
            # a few descriptors is kilobytes; the cap is a refusal of the
            # shape that is not a descriptor file at all.
            text = handle.read(HARNESSES_FILE_MAX_BYTES + 1)
    except FileNotFoundError:
        return (), ()
    except SandboxCeilingUnsealable:
        return (
            (),
            (
                (
                    "",
                    [
                        "harnesses file is not a lone regular file (a link, or a second "
                        "hardlink); the sealed name must be the only name, so nothing is served"
                    ],
                ),
            ),
        )
    except OSError as exc:
        return (), (("", [f"harnesses file could not be read: {exc}"]),)
    if len(text) > HARNESSES_FILE_MAX_BYTES:
        return (
            (),
            (
                (
                    "",
                    [
                        f"harnesses file exceeds {HARNESSES_FILE_MAX_BYTES} bytes; "
                        f"a descriptor file is a handful of small objects"
                    ],
                ),
            ),
        )

    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        # json.JSONDecodeError is a ValueError; the duplicate-key hook raises a
        # plain one. Both are "this file does not say one thing".
        return (), (("", [f"harnesses file is not valid JSON: {exc}"]),)

    if not isinstance(parsed, Mapping):
        return (
            (),
            (
                (
                    "",
                    [
                        "harnesses file must be a JSON object mapping harness id "
                        f"to descriptor, not {type(parsed).__name__}"
                    ],
                ),
            ),
        )
    if len(parsed) > HARNESSES_MAX_DESCRIPTORS:
        return (
            (),
            (
                (
                    "",
                    [
                        f"harnesses file names {len(parsed)} descriptors; at most "
                        f"{HARNESSES_MAX_DESCRIPTORS} are served"
                    ],
                ),
            ),
        )

    valid: list[HarnessDescriptor] = []
    invalid: list[tuple[str, list[str]]] = []
    taken_ids: list[str] = []
    for position, (raw_id, raw_descriptor) in enumerate(parsed.items(), start=1):
        harness_id = str(raw_id)
        descriptor, reasons = descriptor_from_mapping(
            raw_descriptor, harness_id=harness_id, taken_ids=tuple(taken_ids), position=position
        )
        if descriptor is None:
            # Listed under its file position, not its key: the key of an invalid
            # entry is operator text that may be the very mistake (a secret pasted
            # where an id belongs), and the listing is readable by every
            # authenticated dashboard caller.
            invalid.append((synthetic_entry_id(position), reasons))
            continue
        valid.append(descriptor)
        taken_ids.append(descriptor.id)
    return tuple(valid), tuple(invalid)
