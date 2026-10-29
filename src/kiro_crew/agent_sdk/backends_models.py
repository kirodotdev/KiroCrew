"""How a harness switches model and reasoning effort, and which registry namespace its
model ids live in.

The wire channels for a model or effort change, each harness's spelling of the
effort option and its levels, whether the advertised list or Crew's registry
decides, the per-session ``settings.local.json`` that pins a model and the
permission mode it is seeded with, and the model-registry namespace per backend.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

# Records from this owner go to the facade's logger, so a filter or a capture keyed on
# ``kiro_crew.agent_sdk.backends`` keeps seeing them.
logger = logging.getLogger("kiro_crew.agent_sdk.backends")

# Backends that switch models through ``session/set_config_option("model", ...)``
# rather than the kiro-native ``session/set_model`` request. Opt-in for the same
# reason as every capability set: a switch sent down a channel the adapter does not
# implement is answered with method-not-found, and the session keeps serving turns
# on the model the operator thought they had just left.
#
# ``ACP_BACKEND_OPENCODE`` is a member on captured evidence: its ``session/new``
# result advertises a ``model`` select whose ``currentValue`` is the configured
# ``provider/model`` id, and that select is the channel a switch travels down.
#
# ``ACP_BACKEND_PI`` is a member on the same kind of evidence: pi-acp's
# ``session/new`` result advertises a ``model`` select whose ``currentValue`` is the
# ``provider/model`` id pi resolved from its own ``models.json``
# (``test/fixtures/acp_frames/pi/session-live.jsonl``).
#
# ``ACP_BACKEND_DEEPSEEK`` is a member on the same kind of evidence, with one shape
# to respect: its ``model`` select carries OPAQUE values. Each is a JSON-encoded
# ``[provider, model]`` pair -- ``["deepseek-official", "deepseek-v4-flash"]`` -- so
# the value is passed through exactly as advertised rather than parsed. That is what
# ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` membership is for.
ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

#: How a member of the model channel above says it refused a model VALUE, when the
#: JSON-RPC code alone does not say so. pi-acp answers a ``provider/model`` id that
#: pi cannot select -- one absent from the operator's ``models.json``, or whose
#: provider has no key -- with a bare ``-32603 Internal error`` whose details start
#: with this text: its ``setSessionModel`` wraps pi's own ``set_model`` failure.
#: Measured on pi-acp 0.0.34 driving pi 0.87.1. Unread, the refusal is taken for a
#: protocol fault and a stale pin fails the whole session at startup, where every
#: other member stays on its default.
_MODEL_REFUSAL_PHRASE_BY_BACKEND: Mapping[str, str] = {
    ACP_BACKEND_PI: "pi set_model failed:",
}


def model_refusal_phrase(backend: str) -> str:
    """The text *backend* puts in a refused model write, or ``""`` when it has none.

    Read only for the ``model`` option: the phrase names the adapter's model write,
    so it cannot describe a refused effort or any other option.
    """
    return _MODEL_REFUSAL_PHRASE_BY_BACKEND.get(backend, "")


# Backends that take a reasoning-effort change through
# ``session/set_config_option("effort", ...)``. A SEPARATE set from the model
# channel above despite identical membership today: the two config options are
# advertised independently, and ``AcpClient.supports_config_option`` exists
# precisely because adapter builds ship one without the other. Collapsing them
# would make an adapter that gained model-switching inherit an effort channel it
# never advertised.
#
# opencode is NOT a member, which is exactly the split this separate set exists
# for: the same ``session/new`` result that advertises its ``model`` select
# advertises a ``mode`` select beside it and no ``effort`` option at all.
#
# goose IS a member, under its own spelling ``thinking_effort``, and the option is
# PER MODEL. ``test/fixtures/acp_frames/goose/effort-live.jsonl`` carries both halves
# off one live session. On a model goose treats as thinking-capable the select offers
# off, low, medium, high and max, and a write of ``high`` reads back as ``high``. On a
# model with no extended thinking the same select offers ``off`` alone, and a write of
# ``high`` is accepted without error and reads back as ``off``. goose re-advertises the
# select in a ``config_option_update`` after every write and every model switch, so the
# client's stored copy follows the model. The levels it shares with Crew are spelled
# identically; ``xhigh`` has no counterpart and is hidden, as on deepseek. Its
# ``-32602`` was checked on the same build: a value it cannot parse, a request missing
# ``configId`` and an unknown option id all answer ``-32602``. Crew's request shape is
# fixed and the id is gated on ``supports_config_option``, so only the first can reach
# ``_is_config_value_rejection``, which reads it correctly as a value refusal.
#
# pi IS a member, and joins under its OWN spelling rather than the default one: the
# option beside its ``model`` select is ``thought_level``, offering off, minimal,
# low, medium and high, plus xhigh and max where the model maps them, and describing
# itself as "Set the reasoning effort for this session".
# ``test/fixtures/acp_frames/pi/session-live.jsonl`` carries that select off a live
# ``session/new`` result, which is the evidence this membership rests on, and
# ``pi/effort-live.jsonl`` the per-model half: max offered and read back on a model
# that maps it, absent on one that does not. A DIFFERENT id is not an absent channel -- resolving the id per harness
# is what ``EFFORT_CONFIG_OPTION_IDS`` below already exists for, and reading the
# difference as absence is what left this harness reporting no effort control at
# all. Its vocabulary differs too, and that half is answered by
# ``EFFORT_CONFIG_OPTION_ABSENT_FOLDS``: membership says the channel exists, one table
# says what to call the OPTION and the other what to write for ``max`` on a model
# that stops at ``xhigh``.
#
# deepseek IS a member: the same ``session/new`` result carries both selects, the
# effort one offering off, low, high and max. It advertises that option under its own
# id, ``reasoning_effort``, which ``EFFORT_CONFIG_OPTION_IDS`` below records --
# membership says the channel exists, the table says what to call it.
#
# kas IS a member, under its own id ``effortLevel`` (category ``thought_level``).
# KAS serves that select once the session has a concrete model -- it is absent on
# ``auto`` and appears in the ``session/set_config_option model=...`` result -- and
# offers the model's own levels (low through max on the Claude 5.x family). It
# reads none of the cli.json ``chat.modelDefaults`` overlay the kiro family spawns
# with, so without this channel a KAS session always ran at the model's built-in
# default. A value KAS does not offer is answered with success and left unchanged
# (probed against kiro-cli 2.28.0), so the step-down ladder never fires on it.
#
# opencode IS a member, under the default ``effort`` id (category ``thought_level``).
# ``opencode acp`` builds that select per model from the model's own variants and
# omits it on a model that has none; a write sets the session's variant, which its
# ``session/prompt`` hands to the model as the request's reasoning effort. A value
# the model does not offer is answered ``-32602`` "effort not found: <value>", so
# the bare code is a value refusal there too. A ``model`` write resets the variant
# to the new model's first one, which is why the live switch re-applies the level.
# ``test/fixtures/acp_frames/opencode/effort-live.jsonl`` is the evidence.
ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    }
)

# Backends whose ADVERTISED model ids are ``<model>[<effort>]`` pairs that the
# ``model`` config option does not accept whole. codex-acp is the member: its
# ``models.availableModels`` is one entry per model x reasoning effort (the
# legacy ``session/set_model`` vocabulary, and what the picker shows), while its
# ``model`` select takes only the bare model and the effort travels down the
# separate ``reasoning_effort`` option. A member's exhausted spelling ladder falls
# through to that two-write split; a non-member's refused bracketed id stays
# refused. Opt-in (harness-parity H13): claude-agent-acp's ``[1m]`` suffix is a
# context window and must reach the wire intact, and opencode's ``provider/model``
# ids carry no suffix at all -- neither may inherit a split it never advertised.
ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS = frozenset({ACP_BACKEND_CODEX})

# The ``configId`` each backend spells its reasoning-effort option with. One home
# for a fact that is per-harness vocabulary, not a constant: claude-agent-acp
# advertises ``effort``, codex-acp advertises ``reasoning_effort``, KAS advertises
# ``effortLevel``, pi-acp advertises ``thought_level`` and goose advertises
# ``thinking_effort`` (opencode takes the default ``effort``), and a session that
# writes another one's spelling is answered with "unknown config option" and
# silently keeps whatever effort it already had.
#
# Opt-in by exception (harness-parity H13): the default is the ``effort`` spelling
# every existing member of ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` runs through,
# and a backend only appears here to name a different one.
#
# The table and the default are read ONLY by :func:`effort_config_option_id`, and
# no consumer imports either: a consumer indexing the mapping gets a ``KeyError`` for
# every backend without a row, which is the whole failure the resolver exists to
# prevent. The function is the export.
EFFORT_CONFIG_OPTION_IDS: Mapping[str, str] = {
    ACP_BACKEND_CODEX: "reasoning_effort",
    ACP_BACKEND_DEEPSEEK: "reasoning_effort",
    ACP_BACKEND_GOOSE: "thinking_effort",
    ACP_BACKEND_KAS: "effortLevel",
    ACP_BACKEND_PI: "thought_level",
}

#: The spelling used by every backend without a row in
#: ``EFFORT_CONFIG_OPTION_IDS``.
DEFAULT_EFFORT_CONFIG_OPTION_ID = "effort"

# Backends whose ADVERTISED effort option answers two questions Crew's model
# registry answers everywhere else: whether this session takes an effort level at
# all, and which levels may be written. A member advertises the option itself --
# per session, or per model where the harness rebuilds it (goose) -- so the option
# it last served is the authority and the registry cannot speak for it.
#
# Membership is what makes the channel above REACHABLE, and the two are separate
# claims rather than one: ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` says a change
# travels as ``session/set_config_option``, and this set says who decides there is
# a level to send. A harness in the first and not the second is asked
# ``model_supports_effort``, which is a NAME test -- it answers True for the
# Claude and GPT families and False for everything it does not recognise, so on a
# harness serving the operator's own model ids it answers False for every ordinary
# session and the effort control never appears.
#
# pi is a member: its ids are ``provider/model`` pairs out of the operator's own
# ``models.json`` (``ollama/llama3.2:3b`` in
# ``test/fixtures/acp_frames/pi/session-live.jsonl``), which no registry entry and
# no name heuristic carries, while the ``thought_level`` select sits on the same
# ``session/new`` result for all of them.
#
# deepseek is a member for the same reason: its model ids are equally foreign to
# the registry, and its own ``reasoning_effort`` select sits on every
# ``session/new`` result. Its vocabulary omits two of Crew's levels -- it offers
# off, low, high and max, so ``medium`` and ``xhigh`` have no counterpart -- and
# those two are HIDDEN rather than folded: the dropdown is filled from what the
# session advertised, so it never offers them, the dashboard refuses either one
# before it reaches the session (``AcpProvider.accepts_effort_level``), and the
# push's step-down skips any rung the session did not advertise. Folding them
# onto a neighbour would apply a level the user did not pick while the UI reports
# the one they did; offering them unmapped would write a value the harness never
# advertised. The levels it does share with Crew are spelled identically, so
# ``EFFORT_CONFIG_OPTION_VALUES`` carries no deepseek row.
#
# goose is a member because its select is the only thing that knows which of its
# models think: goose decides that itself, per model, and serves the operator's own
# provider ids. A select offering ``off`` alone is goose saying the current model
# takes no level, and ``AcpProvider.supports_effort`` reads it that way rather than
# offering a control whose every write reads back as ``off``. Like deepseek it omits
# ``xhigh``, which is hidden the same way, so it needs no values row either.
#
# opencode is a member for goose's reason: it serves the operator's own
# ``provider/model`` ids and decides per model which ones take a level, offering
# only that model's variants (``low``, ``medium`` and ``high`` on an
# OpenAI-compatible reasoning model) and no select at all on a model without them.
# A level the model does not offer is hidden; its shared levels are spelled as
# Crew spells them, so it needs no values row.
#
# The kiro family and claude are NOT members, and that is the split this set
# exists for: there the level rides the MODEL. kiro-cli refuses effort with
# "Effort configuration is currently not available on <model>", and
# claude-agent-acp rebuilds its effort options per model from
# ``supportedEffortLevels`` -- so the registry, which knows which model families
# take a level, is the right authority for them.
ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION = frozenset(
    {ACP_BACKEND_DEEPSEEK, ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE, ACP_BACKEND_PI}
)


def effort_config_option_id(backend: str) -> str:
    """The ``configId`` *backend* exposes its reasoning effort under.

    Every effort site -- the dashboard's live change, the startup application of
    a persisted slot level, the knowledge pool's apply, the level reader that
    fills the dropdown, and the effort half of a ``<model>[<effort>]`` pick --
    resolves the id here. Two spellings of the same option in one tree diverge
    silently: a write to the wrong id draws "unknown config option", which every
    one of those callers treats as "this adapter has no effort selector" and
    skips, so the session runs an effort the UI does not report.
    """
    return EFFORT_CONFIG_OPTION_IDS.get(backend, DEFAULT_EFFORT_CONFIG_OPTION_ID)


# What each backend calls a LEVEL, where its own vocabulary omits one of Crew's.
# The sibling of ``EFFORT_CONFIG_OPTION_IDS`` and kept beside it: that table answers
# what to call the OPTION, this one what to call the value written into it, and both
# are per-harness vocabulary rather than a constant.
#
# Asked only where the two vocabularies genuinely differ, so most harnesses have no
# row, and today none does. pi had one, folding ``max`` onto ``xhigh`` while its
# capture topped out at ``xhigh``; pi 1.3.0 offers ``max`` itself on a model that
# maps it (``test/fixtures/acp_frames/pi/effort-live.jsonl``), and the fold sent
# ``xhigh`` there when the user picked ``max``. pi spells every level it shares with
# Crew identically, so it needs no row here; the one level it folds, and only where
# the model lacks it, is in ``EFFORT_CONFIG_OPTION_ABSENT_FOLDS`` below. Either way
# the push writes only an advertised value, so no refusal shape has to be read.
EFFORT_CONFIG_OPTION_VALUES: Mapping[str, Mapping[str, str]] = {}

# A fold taken only when the session's advertised list LACKS the level and HAS the
# fold's target. pi's models before 1.3.0, and a 1.3.0 model that maps ``xhigh`` but
# not ``max``, top out at ``xhigh``; a ``max`` saved for such a model ran at ``xhigh``
# and must keep doing so, rather than being hidden and leaving the session on pi's
# default while the slot still records ``max``. Every other missing level stays
# hidden, and where ``max`` is advertised it is written as ``max``.
EFFORT_CONFIG_OPTION_ABSENT_FOLDS: Mapping[str, Mapping[str, str]] = {
    ACP_BACKEND_PI: {"max": "xhigh"},
}


def effort_config_option_value(
    backend: str, level: str, advertised: list[str] | None = None
) -> str:
    """The value *backend*'s effort option spells Crew's *level* with.

    The value-side twin of :func:`effort_config_option_id`, read by the same sites
    for the same reason: the dashboard's live change, the startup application of a
    persisted slot level, the knowledge pool's apply, and the effort half of a
    ``<model>[<effort>]`` pick. One site resolving the level while another writes it
    raw is the same silent divergence two spellings of the option id produce -- the
    session runs a level the UI does not report, or the write is refused and read as
    "this adapter has no effort selector".

    A backend without a row, and a level a backend already spells the same way, come
    back unchanged, so a harness whose vocabulary matches Crew's is untouched.

    *advertised* is the session's own list of values, where the caller has one. A
    value it lacks takes the backend's ``EFFORT_CONFIG_OPTION_ABSENT_FOLDS`` row when
    that row's target IS advertised; otherwise the value comes back as spelled.
    """
    value = EFFORT_CONFIG_OPTION_VALUES.get(backend, {}).get(level, level)
    if advertised and value not in advertised:
        fold = EFFORT_CONFIG_OPTION_ABSENT_FOLDS.get(backend, {}).get(value)
        if fold is not None and fold in advertised:
            return fold
    return value


# Values a backend's effort select offers that mean "no override", not a level.
# opencode 1.19 appends ``default`` to every reasoning model's variants
# (``test/fixtures/acp_frames/opencode/effort-live.jsonl``); Crew's own no-override
# state is the cleared slot level (``clear_effort``), so the value is dropped from
# the advertised levels rather than offered, accepted or stepped down onto.
# claude-agent-acp also offers ``default``, but outside
# ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` the registry, not the advertised
# list, decides what it may be sent, so it needs no row.
EFFORT_CONFIG_OPTION_NO_OVERRIDE_VALUES: Mapping[str, frozenset[str]] = {
    ACP_BACKEND_OPENCODE: frozenset({"default"}),
}


def effort_config_option_levels(backend: str, values: list[str]) -> list[str]:
    """*values* advertised by *backend*'s effort option, minus its no-override values."""
    hidden = EFFORT_CONFIG_OPTION_NO_OVERRIDE_VALUES.get(backend, frozenset())
    return [value for value in values if value not in hidden]


# Backends that resolve the WIRE model id from the provider's OWN advertised list
# (captured from ``session/new`` and cached across sessions) rather than trusting
# the stored id verbatim. Needed where the spelling a backend SERVES differs from
# the one Crew stored: claude-agent-acp advertises versioned ``…[1m]`` ids whose
# bare form collapses to the base (200K) context window. A member both FEEDS the
# advertised-model cache on capture and FOLDS the id onto it — at spawn and on a
# warm-pool ``set_model`` — so a switched model lands on the served spelling.
# Opt-in (harness-parity H6): a future adapter with the same spelling gap joins
# here; one whose wire ids are already exact (kiro-cli serves its ids verbatim and
# gets windows from the ``--list-models`` cache) never needs to.
#
# ``ACP_BACKEND_CODEX`` is a member for the OTHER half of what membership buys:
# the capture. codex-acp advertises its model list only as a ``configOptions``
# ``model`` select on ``session/new``, and that list is the ONLY source of ids
# ``session/set_config_option("model", ...)`` accepts -- the static registry has no
# codex namespace, and kiro-cli's ``--list-models`` catalog names models codex
# refuses with a bare ``-32602``. Without membership the capture skipped the
# select, the picker showed kiro's catalog, and a pick from it killed the session
# at startup. Its spelling fold is a no-op (codex serves its ids verbatim), which
# is fine: the cache it feeds is what the picker reads back.
#
# ``ACP_BACKEND_OPENCODE`` is a member for the capture half as well. Its ids are
# ``provider/model`` pairs it resolves from its own config -- ``ollama/qwen3:8b``
# for a local model, ``opencode/…`` for its hosted ones -- so the advertised select
# is the only vocabulary its ``session/set_config_option`` accepts, and the static
# registry names none of them.
#
# ``ACP_BACKEND_PI`` is a member for the capture half too, and its ids come from
# the same place opencode's do: ``provider/model`` pairs out of the operator's own
# ``models.json`` (``ollama/llama3.2:3b`` for a local model), which no static
# registry names.
#
# ``ACP_BACKEND_DEEPSEEK`` is a member for the capture half, and its ids make the
# capture the ONLY workable source rather than merely the best one: the select's
# values are JSON-encoded ``[provider, model]`` pairs drawn from the harness's live
# service catalog. Nothing can spell one of those from a stored bare model name, so
# a pick that did not come from the capture is a pick the session refuses.
#
# ``ACP_BACKEND_COPILOT`` is a member for the capture half, and for what membership
# keeps OFF its picker. Copilot CLI 1.0.100 advertises no model select and no
# ``models`` block on ``session/new``; ``/model list`` answered "only Auto mode is
# available on your plan" on the recording account. A non-member's picker is read
# from kiro-cli's ``--list-models`` catalog, which names models this harness does not
# serve, so membership is what makes ``GET /api/models`` answer from copilot's own
# namespace -- ``auto`` alone until a session advertises more, which a session on a
# plan with model choice can, through the same capture every member uses.
ACP_BACKENDS_ADVERTISED_MODEL_SELECTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
    }
)

# Backends that seed a per-session settings file — claude-agent-acp's
# ``settings.local.json`` — to lock the model + permission surface. The file is
# written once at spawn, but a warm-pool claim switches model on a process that
# has ALREADY read it, so a member must RE-SEED it on ``set_model``: the
# spawn-time write alone leaves a stale allowlist/model behind, which is what let
# a switched model collapse to its base window on a claimed pool runtime. Opt-in
# for the same reason as every set here — a harness with no such file is not a
# member and takes no re-seed.
#
# opencode is NOT a member, and the reason is not that nothing is supplied to it:
# this set drives the RE-SEED on ``set_model``, because claude's
# ``settings.local.json`` pins the model. What opencode is handed is inline config in
# its own environment carrying ``permission`` alone, and its model travels as a
# config option, so a claim that switches model leaves nothing stale to re-seed --
# and there is no file of Crew's in the work dir at all.
#
# deepseek is NOT a member, and for it the answer is simpler than for either
# harness above: Crew writes no file for it at all. What it supplies to the child is
# one pinned environment variable, and its model travels as a config option, so a
# warm-pool claim that switches model leaves nothing anywhere to re-seed.
ACP_BACKENDS_SEED_LOCAL_SETTINGS = frozenset({ACP_BACKEND_CLAUDE})

#: Operator lever for the permission mode a seeded session runs under, read by
#: :func:`resolve_cc_permission_mode`. One name and one resolver, so a per-session
#: lever and an operator-wide one cannot disagree about the same session.
CC_PERMISSION_MODE_ENV = "KIROCREW_CC_PERMISSION_MODE"


def resolve_cc_permission_mode(explicit: str | None, backend: str) -> str | None:
    """The ``permissions.defaultMode`` a session seeds, or ``None`` to seed nothing.

    Two opt-ins, one answer: the caller's own per-session request (the dashboard
    slot's Auto intent) first, then :data:`CC_PERMISSION_MODE_ENV` for an operator
    who wants every session on the backend's classifier. ``None`` leaves the
    seeded ``settings.local.json`` without a ``defaultMode`` key at all, which is
    the backend's own per-tool default -- so with nothing asking, nothing widens.

    Fail-closed in both axes. A backend that seeds no per-session settings file
    gets ``None``, asked of :data:`ACP_BACKENDS_SEED_LOCAL_SETTINGS` rather than of
    the backend id, so a future seeding adapter joins the set instead of editing
    this. And ``auto`` EXACTLY is the only value that resolves: a typo, a stray
    space, a stale value or an inherited ``bypassPermissions`` resolves to ``None``
    rather than to a wider surface than the one it names.

    An unrecognised value is not silently dropped, because that is the failure this
    whole path was reported for: a lever an operator believes is set, doing nothing,
    with every other signal reading healthy. It warns and names what it read.
    """
    # Function-scope import, not module-scope: this module is on the load path of
    # ``KiroCrewConfig.load()`` and stays free of ``kiro_crew.acp`` there (see the
    # import-light note in ``kiro_crew.acp_backends``; ``test_acp_capability_sets_leaf``
    # pins it in a subprocess).
    from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO

    if backend not in ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        return None
    requested = explicit or os.environ.get(CC_PERMISSION_MODE_ENV) or ""
    if requested == CC_PERMISSION_MODE_AUTO:
        return CC_PERMISSION_MODE_AUTO
    if requested:
        logger.warning(
            "permission mode %r is not recognised (only %r is); this session runs on "
            "the backend's own per-tool default",
            requested,
            CC_PERMISSION_MODE_AUTO,
        )
    return None


# Which model-registry NAMESPACE a backend's ids live in. This is a registry index
# key, NOT a provider-identity check (see agent_sdk.provider_identity, note 3): a
# context window is a property of the MODEL, so the same model reached via two
# backends shares one namespace. Two consumers read it, which is why every known
# backend is mapped rather than only the members of one set: the wire-id fold for an
# ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` member picks its registry index here,
# and :func:`kiro_crew.agent_sdk.capabilities.capabilities_for` reads it for EVERY
# backend to fill ``SessionCapabilities.model_id_namespace``. Defaults to the
# ``acp`` (kiro) namespace, where
# every non-claude id the registry carries lives today. The literals are the
# model_registry's own provider keys, spelled here rather than imported to keep
# this load-path leaf free of a ``kiro_crew.model_registry`` dependency.
#
# ``ACP_BACKEND_CODEX`` gets its OWN key. The same key also selects the bucket of
# the cross-session advertised-model cache (``model_registry.advertised_models``),
# and codex's served ids (``gpt-5.4``, ``gpt-5.4-codex``, ...) are a different
# vocabulary from the kiro ids that live under ``acp``. Sharing the bucket would
# let a codex session overwrite what the picker offers for a kiro-family harness.
# The static registry has no ``codex`` provider, so the translation into this
# namespace is a passthrough -- which is exactly right for ids the backend itself
# advertised.
_MODEL_REGISTRY_NAMESPACE_BY_BACKEND: dict = {
    ACP_BACKEND_CLAUDE: "claude_code",
    ACP_BACKEND_KIRO: "acp",
    ACP_BACKEND_KAS: "acp",
    ACP_BACKEND_CODEX: "codex",
    # opencode gets its own key for the same reason codex does: its ids are
    # ``provider/model`` pairs drawn from the operator's own provider list, so
    # sharing the ``acp`` bucket would let one harness overwrite what the picker
    # offers for another.
    ACP_BACKEND_OPENCODE: "opencode",
    # pi likewise: ``provider/model`` pairs from the operator's own models.json.
    ACP_BACKEND_PI: "pi",
    # goose likewise, and it is the sharpest case: its ``session/new`` advertises the
    # whole provider catalog it could reach, so sharing the ``acp`` bucket would not
    # merely mix two harnesses' ids -- it would replace kiro-cli's advertised catalog
    # with a list of every provider goose knows about.
    ACP_BACKEND_GOOSE: "goose",
    # deepseek gets its own key on the strongest form of the same reason: its ids are
    # JSON-encoded ``[provider, model]`` pairs from its own catalog, a vocabulary no
    # other harness spells, so a shared bucket would offer the picker ids that only
    # one backend can accept.
    ACP_BACKEND_DEEPSEEK: "deepseek",
    # copilot gets its own key: its ids are the Copilot service's catalog
    # (``auto``, ``claude-sonnet-4.5``, ``gpt-5.4`` ...) as that account is entitled
    # to them, so sharing the ``acp`` bucket would let a Copilot session replace the
    # list the picker offers for kiro-cli with models kiro-cli cannot serve.
    ACP_BACKEND_COPILOT: "copilot",
}


def model_registry_namespace(backend: str) -> str:
    """The model-registry namespace key for *backend* (default ``acp``)."""
    return _MODEL_REGISTRY_NAMESPACE_BY_BACKEND.get(backend, "acp")
