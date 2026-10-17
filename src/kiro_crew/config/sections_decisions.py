"""The ``decisions`` section: the decision provider, wake judge, model routes and their bounds.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, _safe_bool, _safe_int
from kiro_crew.config.sections_agent_fields import normalize_agent_model
from kiro_crew.config.sections_defaults import SectionReader

# Sampling bucket bounds, as a percentage of sessions. 0 admits nothing, 100
# admits everything; both the parse below and the gate clamp to this range rather
# than treating an out-of-range value as a second way to disable the seam.
DECISION_BUCKET_MIN = 0
DECISION_BUCKET_MAX = 100

DECISION_PROVIDER_ENDPOINT_DEFAULT = "https://api.typesafe.ai/v1/systemone"
DECISION_PROVIDER_MODEL_DEFAULT = "jev-latest"

# How much PRIOR CONVERSATION one decision may carry, as a char budget rather than
# a message count, because what it bounds is the size of the request that leaves
# the machine -- a count bounds neither.
#
# The default covers the last two or three turns, which is what a request like "do
# the B comparison drawer" needs for the oracle to know what "B" names. It widens
# nothing on its own: the gate sends ``min(this, the consent keystone's ceiling)``,
# and that ceiling is 0 until the owner reviews a prior-turn number, so an install
# whose consent names only the message excerpt and the candidate descriptions
# carries the current message alone.
DECISION_HISTORY_BUDGET_DEFAULT = 2000

# The tiers ``model.route`` may answer with, and the model each maps to by default.
# The keys are the point's CLOSED answer domain
# (``decisions.points.model_route.TIERS``): a key outside it is dropped, because a
# tier the question never offers can never be answered and a map that accepted one
# would read as configured while routing nothing.
#
# The values are ordinary model ids and grant nothing on their own -- the point
# validates each against what the provider advertises to this account and keeps the
# session's current model when an id is not there -- so this stays a config value
# rather than a keystone one.
DECISION_MODEL_ROUTE_TIERS: tuple[str, ...] = ("simple", "medium", "complex")

# Every tier defaults to ``""`` -- INHERIT, i.e. the turn keeps the model its
# session is already on. No concrete model id is named here, and none may be: a
# hardcoded id fails at runtime -- silently, until the first prompt -- for every
# account not entitled to it, so
# ``docs/system-specs/common/model-selection.md`` allows ids to be pinned only in
# an operator-written map and keeps code defaults at ``""`` / ``"auto"``.
# ``code-review.yml`` gates on it.
#
# The three keys are PRESENT and empty rather than absent, which is the same
# shape ``agent.role_models``'s roles take: "this tier exists and is unpinned" is
# a state the log and the strip report ("complex -> (unpinned)"), so it needs a
# spelling of its own rather than being inferred from a missing key.
DECISION_MODEL_ROUTE_DEFAULT: dict[str, str] = {tier: "" for tier in DECISION_MODEL_ROUTE_TIERS}


def coerce_model_route(raw: object) -> dict[str, str]:
    """Normalize ``decisions.model_route`` from a hand-edited config.

    Always returns all three tiers. Each value goes through
    :func:`normalize_agent_model`, exactly as :func:`coerce_role_models` does, so
    ``"auto"`` and a non-string both collapse to ``""`` -- "inherit" has ONE
    spelling, and a tier set to ``"auto"`` keeps inheriting instead of hard-pinning
    the backend's own default.

    A tier outside the three is dropped: the question never offers it, so it could
    never be answered, and a map that accepted one would read as configured while
    routing nothing.
    """
    section = raw if isinstance(raw, dict) else {}
    return {tier: normalize_agent_model(section.get(tier)) for tier in DECISION_MODEL_ROUTE_TIERS}


# The providers ``decisions.nudge_wake.provider`` may name. ``auto`` resolves at
# decision time -- Jev when the keystone consents to it, the LLM lane otherwise --
# so a machine that later gains or loses a Jev key needs no config edit.
JUDGE_PROVIDER_AUTO = "auto"
JUDGE_PROVIDER_JEV = "jev"
JUDGE_PROVIDER_LLM = "llm"
JUDGE_PROVIDERS = (JUDGE_PROVIDER_AUTO, JUDGE_PROVIDER_JEV, JUDGE_PROVIDER_LLM)


@dataclass
class NudgeWakeConfig:
    """Per-point settings for the wake judge (``decisions`` point ``nudge.wake``).

    Deliberately carries NO ``enabled``. There is no feature toggle, because the two
    lanes are authorized by different things and neither of them is a toggle:

    * The Jev lane needs the Decisions keystone in full -- the main switch AND this
      point's own ``nudge_evidence`` scope, because that scope names a category of
      egress to the Jev endpoint. Config cannot grant it and this section cannot
      widen it.
    * The ``llm`` lane needs no consent row, so ``provider = llm`` plus a ``judge``
      spec on the loop is what runs it. What makes that safe is not that config is
      trusted: the lane adds no destination and no data class. It sends to the model
      provider the owner's sessions already send to every turn, carrying a scrubbed,
      bounded subset of the owner's own children's transcripts, which that provider
      already received when those sessions ran. The judge only chooses QUIET against
      firing and is fail-open, so the worst case is one delayed wake, bounded by the
      quiet-streak floor.

    Hot-applied: the gate reads the live snapshot per call.
    """

    provider: str = field(
        default=JUDGE_PROVIDER_AUTO,
        metadata=_meta(
            "Judge provider",
            "Which judge answers at nudge.wake: 'jev' (the System One model this "
            "card's consent switch covers), 'llm' (a small text-only model on the "
            "provider this machine already uses, no extra key needed), or 'auto' "
            "-- Jev when consent stands for it, otherwise the small model. "
            "Anything else reads as 'auto'. Choosing 'jev' without that consent "
            "sends nothing and every tick fires as it does today.",
        ),
    )
    llm_model: str = field(
        default="",
        metadata=_meta(
            "Judge model (LLM lane)",
            "The model id the 'llm' lane runs on, spelled as your provider "
            "advertises it. EMPTY INHERITS: the judge keeps the model its "
            "background agent already resolves, which is the default. A value that "
            "is not a short model id is ignored rather than sent.",
        ),
    )

    quiet_streak_floor: int = field(
        default=0,
        metadata=_meta(
            "Quiet ticks before firing anyway",
            "How many QUIET verdicts in a row end the run of skipped turns. The Nth "
            "consecutive QUIET tick FIRES regardless of its own verdict, so this many "
            "quiet verdicts skip one fewer turn than the number suggests, and a judge "
            "that is wrong about a subject costs a late turn rather than silence. 0, "
            "the default, means inherit the shipped floor -- the same number the typed "
            "probe path uses, spelled once in the loop engine so the two cannot drift. "
            "A negative value also reads as inherit. This knob only ever SHORTENS the "
            "silence window: the shipped floor is the ceiling, and a larger value is "
            "clamped back down to it, because config.json is writable by an "
            "auto-approved agent shell and a raised floor would silence a watch with "
            "no new code, only a number.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "NudgeWakeConfig":
        """Normalize rather than reject, the posture the whole section takes.

        Every unreadable value resolves to the shipped default. Neither key can reach
        a destination the session does not already send to, so a hand-edit that fails
        to parse costs a preference, not a permission.
        """
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        raw_provider = section.get("provider")
        provider = raw_provider.strip().lower() if isinstance(raw_provider, str) else ""
        raw_model = section.get("llm_model")
        return cls(
            # An unknown name reads as ``auto`` rather than as an error: a typo must
            # not become a third lane and must not stop the gateway booting.
            provider=provider if provider in JUDGE_PROVIDERS else reader.default("provider"),
            # Kept verbatim (stripped) and validated where it is USED, against
            # ``decisions.types.MODEL_ID_RE``: storing "" for an id this build
            # cannot use would make the saved config disagree with what the operator
            # wrote, and the bound that matters is at the call that names a model.
            llm_model=(
                raw_model.strip() if isinstance(raw_model, str) else reader.default("llm_model")
            ),
            # Absent, malformed and negative all read as 0, which the engine resolves
            # to its shipped floor. The ceiling is NOT clamped here: it is the engine's
            # own constant, and importing it would invert this module's dependency on
            # the loop engine (which imports ``config.loader`` at module scope). The
            # engine clamps on every read, so an over-large value never takes effect.
            quiet_streak_floor=reader.read("quiet_streak_floor", _safe_int, 0),
        )


@dataclass
class DecisionProviderConfig:
    """Where the System One provider lives and what one call may cost."""

    endpoint: str = field(
        default=DECISION_PROVIDER_ENDPOINT_DEFAULT,
        metadata=_meta(
            "Endpoint",
            "Full URL of the evaluation endpoint. Override only to point at a "
            "compatible proxy — the request and response field names are fixed by "
            "the TypeSafe API, not by this setting.",
        ),
    )
    api_key: str = field(
        default="secret://TYPESAFE_API_KEY",
        metadata=_meta(
            "API Key",
            "The provider credential reference. Only 'secret://TYPESAFE_API_KEY' is "
            "honoured: it reads that one entry from the dashboard secrets vault "
            "(Settings › Secrets), so no key is ever stored in config.json. A literal "
            "key here is NOT used, and no other vault entry is readable through this "
            "field, because config.json is agent-writable. With no usable key the "
            "seam logs a row saying so and returns None — it never sends an empty "
            "bearer credential.",
            sensitive=True,
        ),
    )
    model: str = field(
        default=DECISION_PROVIDER_MODEL_DEFAULT,
        metadata=_meta(
            "Model",
            "Provider model id. 'jev-latest' is TypeSafe's flagship System One model. "
            "Letters, digits, dots, dashes and underscores, up to 64 characters; "
            "anything else is refused before a request is sent, because this field "
            "travels in the request and config.json is agent-writable.",
        ),
    )
    timeout_ms: int = field(
        default=1000,
        metadata=_meta(
            "Timeout (ms)",
            "Total budget for one decision, in milliseconds. Exceeding it logs "
            "error='timeout' and returns None, so this is the ceiling the seam "
            "adds to the path it sits in — not a target. Values at or below zero "
            "are floored to 1ms rather than disabling the timeout.",
        ),
    )


@dataclass
class DecisionsConfig:
    """Decision seam (``src/kiro_crew/decisions/``). Off by default.

    There is deliberately NO ``enabled`` field here. The switch that lets
    conversation state leave the machine is an authorization, not a preference,
    and ``config.json`` is writable by an auto-approved agent shell -- so it lives
    on the KEYSTONE leaf ``decisions_consent.json`` (``decisions.consent``), the
    same placement as ``computer_use.json`` and ``aws_service_consent.json``. This
    section carries only the knobs that grant nothing on their own: the sampling
    share, the prior-conversation budget (0 by default, so raising it is a choice),
    the tier-to-model map ``model.route`` reads, and the provider. There is no
    per-point arm and no shadow mode. The shipped points are listed in
    ``DECISION_POINT_NAMES`` (``decisions/gate.py``); each module under
    ``decisions/points/`` owns how its point is reached -- for example a
    non-zero ``skills.max_triggered`` for ``skills.select`` and the picker's
    ``Auto (Jev)`` entry for ``model.route``.

    Every field is hot-applied (no ``restart=True`` anywhere): the gate reads the
    live snapshot per call, so a bucket change takes effect on the next decision
    without a gateway restart.
    """

    bucket: int = field(
        default=DECISION_BUCKET_MAX,
        metadata=_meta(
            "Bucket (%)",
            "Percentage of sessions the seam fires for, 0-100, decided by a hash "
            "of the session key so a session is consistently in or out. 0 fires "
            "for nobody, 100 for everybody. Out-of-range numbers are clamped; a "
            "value that is not a whole number reads as 0 (nobody sampled), never "
            "as everybody. To switch the seam off, withdraw consent in Settings > "
            "Developer > Feature Previews, not a zero bucket.",
        ),
    )
    history_budget_chars: int = field(
        default=DECISION_HISTORY_BUDGET_DEFAULT,
        metadata=_meta(
            "History budget (chars)",
            "How many characters of PRIOR conversation one decision may carry, on "
            "top of the current message. Earlier user and assistant turns are added "
            "newest-first until this many characters are spent and the last one is "
            "clipped to fit; tool output is never sent. The default is 2000, about "
            "the last two or three turns, and the consent keystone caps it: the gate "
            "sends the smaller of the two, so an install whose owner reviewed no "
            "prior-turn ceiling sends no prior turns at all. A negative value reads "
            "as 0.",
        ),
    )
    model_route: dict[str, str] = field(
        default_factory=lambda: dict(DECISION_MODEL_ROUTE_DEFAULT),
        metadata=_meta(
            "Model per difficulty tier",
            "Which model answers a chat turn Jev put in each difficulty tier, for "
            "a session whose model is set to 'Auto (Jev)' in the chat model "
            "picker. Keys are the three tiers the question offers -- 'simple', "
            "'medium', 'complex' -- and each value is a model id the provider "
            "advertises to your account, exactly as the chat model picker spells "
            "it. Every tier is EMPTY by default, which means inherit: the turn "
            "keeps the model its session is already on, and the decision is still "
            "recorded so you can see which tier Jev chose before you pin anything. "
            "No model id is named for you, because an id your account is not "
            "offered would fail on the first prompt. 'auto' means the same as empty. "
            "An id your account cannot run keeps the session's model too and "
            "records why in the decision log. Routing a turn to a dearer model "
            "costs more, which is why it happens only for a session whose owner "
            "picked 'Auto (Jev)' -- a manual model choice is never overridden.",
        ),
    )
    model_route_derive: bool = field(
        default=False,
        metadata=_meta(
            "Derive tier models",
            "Fill each EMPTY tier of 'Model per difficulty tier' from the models your "
            "account is offered right now, using their credit multipliers: 'simple' "
            "runs on the cheapest model priced below the session's own model, "
            "'medium' keeps the session's own model, and 'complex' runs on the "
            "dearest model priced above it. A tier you pinned always wins. With this "
            "on, a sub-agent spawned with no model of its own is routed the same "
            "way once tool-call arguments are also allowed on the Decisions card "
            "(its task is one), and a chat turn you regenerate, or the turn after one that hit "
            "its length or step limit, runs one tier higher. Off by default. No "
            "model id is written for you: the targets "
            "are read from the live model list each time, and a tier with no "
            "price to compare (a cold list, a tie) keeps the session's model. The "
            "decision log marks every derived choice 'derived'.",
        ),
    )
    provider: DecisionProviderConfig = field(
        default_factory=DecisionProviderConfig,
        metadata=_meta("Provider", "Where decisions are sent and what they may cost."),
    )
    nudge_wake: NudgeWakeConfig = field(
        default_factory=NudgeWakeConfig,
        metadata=_meta(
            "Wake judge",
            "Per-point settings for nudge.wake: which judge answers, the model "
            "id for the small-model lane, and how long a judge may keep a loop "
            "quiet before one fires anyway. The Jev lane still needs this point's "
            "consent scope on the Decisions card; the small-model lane needs no "
            "consent row, because it sends to the model provider your sessions "
            "already use, so picking it here is what runs it.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "DecisionsConfig":
        """Build from a raw ``decisions`` dict -- the ONE parse site.

        Lives here rather than in the loader because the bucket bounds are
        declared a few lines above, and a normalizer that read them from another
        module would need those names re-exported across a frozen module boundary
        (``test_config_module_boundaries``).

        Every value is NORMALIZED rather than validated-and-rejected: this
        section gates a seam that is off by default, so the fail-closed reading
        of any unreadable value is the default, and a hand-edited config.json
        must not stop the gateway booting.

        Accepts whatever ``json.loads`` produced, including ``None`` and a
        non-dict, for the same reason ``ResourceLimitsConfig.from_raw`` does. A
        config carrying the earlier ``preview``/``points``/``enabled`` spelling
        is read for its bucket and provider only: consent is never inferred from
        ``config.json``, whatever key it carries, because that file is what the
        keystone exists to keep the decision out of.
        """
        if not isinstance(section, dict):
            return cls()

        reader = SectionReader(cls, section)
        raw_provider = section.get("provider")
        raw_provider = raw_provider if isinstance(raw_provider, dict) else {}

        def _text(key: str, default: str) -> str:
            """A non-empty stripped string, else *default*.

            An empty or blank value resolves to the DEFAULT rather than to ``""``:
            the implementation falls back to the documented endpoint and model
            anyway, so storing ``""`` would leave the saved config disagreeing
            with what is in force -- the same reason ``bucket`` is clamped here.
            """
            raw = raw_provider.get(key)
            return raw.strip() if isinstance(raw, str) and raw.strip() else default

        provider = DecisionProviderConfig(
            endpoint=_text("endpoint", DecisionProviderConfig.endpoint),
            api_key=_text("api_key", DecisionProviderConfig.api_key),
            model=_text("model", DecisionProviderConfig.model),
            timeout_ms=_safe_int(
                raw_provider.get("timeout_ms", DecisionProviderConfig.timeout_ms),
                DecisionProviderConfig.timeout_ms,
            ),
        )

        return cls(
            # Clamped here as well as in the gate. The gate clamps because it
            # must never trust a value it did not parse; clamping here is what
            # makes the SAVED config say what is in force, so an operator who
            # wrote 500 sees 100 come back rather than a number that behaves as
            # 100 while reading as 500.
            #
            # ABSENT reads as the default (every session); MALFORMED reads as 0.
            # The two must not share a fallback: this number decides how much
            # conversation state leaves the machine, so a hand-edit that fails to
            # parse must fail closed to "nobody", never open to "everybody".
            bucket=_safe_int(
                section.get("bucket", DECISION_BUCKET_MAX),
                DECISION_BUCKET_MIN,
                DECISION_BUCKET_MIN,
                DECISION_BUCKET_MAX,
            ),
            # Unreadable reads as the DEFAULT, the same direction a malformed
            # bucket takes. That cannot fail open for this key: the gate holds the
            # configured number against the consent keystone's ceiling, so an
            # unreadable value still sends at most what the owner reviewed. Floored
            # at 0 so a negative number cannot read as unbounded.
            history_budget_chars=reader.read("history_budget_chars", _safe_int, 0),
            # Per-TIER fallback rather than per-map: see `coerce_model_route`. An
            # absent section and one naming no known tier both read as the shipped
            # map, since this key cannot widen anything -- every id is still held
            # against the provider's advertised list at routing time.
            model_route=coerce_model_route(section.get("model_route")),
            # A real bool only: anything else reads as the default, off, so a
            # hand-edit that fails to parse never turns routing on.
            model_route_derive=reader.read("model_route_derive", _safe_bool),
            provider=provider,
            nudge_wake=NudgeWakeConfig.from_raw(section.get("nudge_wake")),
        )
