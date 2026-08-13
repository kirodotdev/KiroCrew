"""Autonomy gate and rotation-driven tier arming.

Two safety decisions live here.

**The autonomy gate** (``resolve_mode`` / ``authorize_action``) decides whether the
agent may write to a user's production tooling. The default is ``observe`` —
nothing is written anywhere. ``act`` requires BOTH an app-level mode of ``act`` AND
a user-authored rule whose predicate matches this specific signal. There is no
wildcard: a rule must name a source and either a resource glob or a label match, so
"act on everything" is not expressible. Autonomy is earned per-pattern by the
operator after they have watched the agent's proposals be correct.

This is a deliberate divergence from a common shortcut: auto-resolving alert types
believed to be always benign. A team that built those alerts itself can reason about
which are safe to close unread; a stranger's first install has no such basis, and
auto-resolving a human's production page on day one is a much worse failure than
being slightly slow.

**Tier arming** (``tier_states``) maps the on-shift answer onto which SOP tiers
run. Note the fail-open: an unreachable rotation API arms the on-shift tier rather
than disarming it. Wrongly arming costs a few API polls; wrongly disarming means
nobody notices an outage.

See ``docs/system-specs/modules/ops-mission-control.md`` (autonomy gate, tier arming).
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    MODE_ACT,
    MODE_OBSERVE,
    MODE_ORDER,
    MODE_PROPOSE,
    VALID_ACTIONS,
    Signal,
    effective_mode,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import ShiftStatus
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

TIER_ALWAYS = "always"
TIER_ON_SHIFT = "on_shift"
TIER_PRIMARY = "primary"

#: Cron names per tier, as the SCHEDULER knows them.
#:
#: These MUST match what app registration actually creates. A manifest cron named
#: ``dispatch`` is registered namespaced as ``ops-mission-control/dispatch`` — so the
#: bare ``omc-*`` names this table used to carry matched no job at all, and every
#: pause/resume the rotation tier emitted silently targeted nothing. The whole tier
#: mechanism was inert. Found by exercising the rotation-check SOP against the real
#: scheduler; pinned by ``test_tier_cron_names_match_the_manifest``.
_CRON_PREFIX = "ops-mission-control"

TIER_CRONS: dict[str, tuple[str, ...]] = {
    # `rotation-check` is the ONLY always-tier job, and it must be: on a gated tier an
    # off-shift instance could never re-arm itself.
    TIER_ALWAYS: (f"{_CRON_PREFIX}/rotation-check",),
    # `reconcile` is ON-SHIFT, not always. It POSTs `incident/transition` and edits the
    # incident's Slack message, so on a team every instance would race to resolve the same
    # incidents and rewrite the same thread — the shared-state mutation the single-owner
    # model exists to prevent, just on the reconciliation path instead of the claim path.
    #
    # It sat on the `always` tier because that read as "keeping the board honest is
    # everyone's job". It is not: the board is shared, so exactly one instance may correct
    # it. A shared-state reconciler belongs on the
    # oncall-gated tier for the same reason.
    TIER_ON_SHIFT: (f"{_CRON_PREFIX}/dispatch", f"{_CRON_PREFIX}/reconcile"),
    TIER_PRIMARY: (f"{_CRON_PREFIX}/ledger-hygiene",),
}

#: Default app-level autonomy. ``observe`` — see the module docstring.
DEFAULT_APP_MODE = MODE_OBSERVE

#: Bound on a companion rotation source's ``on_shift()`` when it offers no sync core. This
#: gate fronts an operator action, so a hung provider must not hold the request open; the
#: timeout is a FAULT (counted off-shift), not an abstention. Matches the 10s ``gh api user``
#: budget on the other blocking path in this gate.
_ASYNC_SHIFT_TIMEOUT_SECS = 10.0

#: Config key holding the user's autonomy rules.
_RULES_KEY = "autonomy_rules"
_APP_MODE_KEY = "mode"

#: Heartbeat-pacing keys, duplicated from ``dispatch`` because importing them would close
#: an import cycle (``dispatch`` imports this module). They are asserted equal to
#: ``dispatch``'s own constants by ``test_store_and_gate.py``, so the duplication cannot
#: drift silently — which is the only reason it is acceptable.
_CONFIG_MAX_CLAIMS = "max_claims_per_cycle"
_CONFIG_STALE_AFTER = "stale_after_secs"
_CONFIG_NEEDS_HUMAN_STALE_AFTER = "needs_human_stale_after_secs"


@dataclass(frozen=True)
class AutonomyRule:
    """One user-authored grant.

    A rule must name a ``source`` AND at least one of ``resource_glob`` /
    ``label_match``. A source-only rule is refused (see ``from_dict``): "act on
    everything CloudWatch reports" is exactly the blanket grant this design
    exists to prevent.
    """

    source: str
    mode: str
    resource_glob: str = ""
    label_match: dict[str, str] | None = None
    actions: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutonomyRule | None:
        source = str(data.get("source", "")).strip()
        mode = str(data.get("mode", "")).strip()
        if not source or mode not in MODE_ORDER:
            return None
        resource_glob = str(data.get("resource_glob", "")).strip()
        raw_labels = data.get("label_match")
        label_match = (
            {str(k): str(v) for k, v in raw_labels.items()}
            if isinstance(raw_labels, dict) and raw_labels
            else None
        )
        # No wildcard grants: a rule that names only a source would authorize
        # every signal from that provider forever.
        #
        # An all-wildcard GLOB is the same grant spelled differently, and it passed the
        # nonempty check above: `resource_glob: "*"` (or `"**"`, `"?"`, `"*?*"`) is
        # truthy, so the rule was accepted and `fnmatch` then matched every resource
        # including the empty string — the provider-wide act grant this design states is
        # inexpressible, reachable from Settings. A glob has to carry at least one
        # LITERAL character to narrow anything; one made only of `*`/`?` narrows nothing
        # and is refused for the same reason omitting it is. Found in review (GPT 5.6).
        # Whitespace is not a literal for this purpose: `"*  *"` would otherwise "narrow"
        # to two spaces, and no resource id is whitespace, so it still matches everything
        # a bare `*` does.
        narrowing_glob = "".join(resource_glob.split()).strip("*?")
        if mode == MODE_ACT and not narrowing_glob and not label_match:
            logger.warning(
                "ops-mission-control: refusing act-rule for %r — a resource_glob of %r "
                "matches everything, so it is a blanket grant (use a glob with at least "
                "one literal character, or a label_match)",
                source,
                resource_glob,
            )
            return None
        # `actions` narrows a grant, and an EMPTY set means "every action" (see
        # `authorize_action`: `not rule.actions or action in rule.actions`). So silently
        # dropping unrecognised verbs inverted the operator's intent: `actions: ["resovle"]`
        # filtered down to nothing and the rule then authorized ack, resolve, comment AND
        # silence — one typo turning a narrow grant into the blanket one this design refuses to
        # let anyone express. Found in review.
        #
        # A malformed narrowing is now a REJECTED rule, not a widened one. `save_rules` surfaces
        # that as a 400 naming the index, so the operator fixes the typo instead of unknowingly
        # running with more authority than they asked for. The key being ABSENT is still
        # "every action" — that is a deliberate choice the operator made by omission, and is
        # what the manual documents.
        if "actions" in data:
            raw_actions = data.get("actions")
            if not isinstance(raw_actions, list) or not raw_actions:
                logger.warning(
                    "ops-mission-control: refusing act-rule for %r — `actions` must be a "
                    "non-empty list (omit the key to grant every action)",
                    source,
                )
                return None
            unknown = [str(a) for a in raw_actions if str(a) not in VALID_ACTIONS]
            if unknown:
                logger.warning(
                    "ops-mission-control: refusing act-rule for %r — unknown action(s) %s; "
                    "an unrecognised verb would silently widen the grant to every action",
                    source,
                    ", ".join(sorted(unknown)),
                )
                return None
            actions = frozenset(str(a) for a in raw_actions)
        else:
            actions = frozenset()
        return cls(
            source=source,
            mode=mode,
            resource_glob=resource_glob,
            label_match=label_match,
            actions=actions,
        )

    def matches(self, signal: Signal) -> bool:
        if self.source != signal.source:
            return False
        if self.resource_glob and not fnmatch.fnmatch(signal.resource, self.resource_glob):
            return False
        if self.label_match:
            for key, expected in self.label_match.items():
                if signal.labels.get(key) != expected:
                    return False
        return True


def app_mode() -> str:
    """Operator-set ceiling on autonomy, defaulting to ``observe``.

    Read from the KEYSTONE policy store ONLY, never ``config.json``: the mode is a security
    ceiling and ``config.json`` is agent-writable (see ``policy_store``). There is no
    migration from config — promoting a value found there would let the agent set its own
    ceiling, which is the hole this fencing exists to close.
    """
    mode = policy_store.read_mode(DEFAULT_APP_MODE)
    return mode if mode in MODE_ORDER else DEFAULT_APP_MODE


def load_rules() -> list[AutonomyRule]:
    # Keystone store ONLY, same reasoning as ``app_mode``: an act-rule is half the
    # authorization, so it cannot be read from where the agent can write it.
    raw = policy_store.read_rules_raw()
    rules: list[AutonomyRule] = []
    for item in raw:
        if isinstance(item, dict):
            rule = AutonomyRule.from_dict(item)
            if rule is not None:
                rules.append(rule)
    return rules


def save_rules(raw: list[Any]) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate and persist act-rules. Returns ``(ok, error_code, normalized)``.

    The write half of ``load_rules``, and it did not exist: `policy_store.set_rules` had no
    caller anywhere, so the app's headline `act` mode had no authoring path at all. The UI
    said "patterns you have explicitly allowlisted with a rule" and then dead-ended at "No
    rules defined yet." with nothing to click, while the manual told operators to edit
    `data/config.json` — which the keystone migration ignores once the policy file exists.
    So every act-mode adopter silently got Propose behavior. Found in review.

    Validation reuses ``AutonomyRule.from_dict`` rather than restating the rules, because
    that is where "an act-rule may not be a blanket grant" lives. A rule it rejects is
    refused with an error here instead of being written and silently dropped on the next
    read — a grant that appears saved but never matches is the failure mode this whole
    two-key design exists to avoid.

    Returns the NORMALIZED list (what was actually stored), so the UI renders the parsed
    rules rather than the operator's submission.
    """
    if not isinstance(raw, list):
        return False, "rules_not_a_list", []

    ok, code, normalized = validate_rules(raw)
    if not ok:
        return False, code, []

    policy_store.set_rules(normalized)
    sel().log_api_access(
        caller="ops-mission-control",
        operation="rotation.save_rules",
        outcome="allowed",
        resources=f"rules={len(normalized)}",
    )
    return True, "", normalized


def validate_rules(raw: list[Any]) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate act-rules WITHOUT persisting. Returns ``(ok, error_code, normalized)``.

    Split out of ``save_rules`` so a caller can check every field in a request before writing
    ANY of it. ``PUT /settings`` needs that: it wrote ``mode`` first and validated
    ``autonomy_rules`` second, so a request carrying a valid ``mode=act`` and a malformed rule
    persisted the mode, returned 400, and left the instance in ``act`` — activating whatever
    grants were already stored, from a request the operator was told had FAILED. Found in
    review. Validation is the same code either way, so the two paths cannot disagree.
    """
    if not isinstance(raw, list):
        return False, "rules_not_a_list", []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return False, f"rule_{index}_not_an_object", []
        rule = AutonomyRule.from_dict(item)
        if rule is None:
            # `from_dict` already logged which invariant failed.
            return False, f"rule_{index}_invalid", []
        # Same serializer the read path uses, so what is stored is exactly what
        # `rules_detail` will hand back to the editor.
        normalized.append(rule_to_dict(rule))
    return True, "", normalized


def sweep_windows() -> dict[str, Any]:
    """The three heartbeat-pacing knobs, resolved exactly as ``run_cycle`` resolves them.

    These are settable through ``PUT /settings`` and were readable back through NOTHING, so
    an operator could change how long a dead investigation pins a signal and then had no
    way to see the value they had set — or to discover the defaults they were living under.
    That is this app's signature failure in miniature: the knob turns, and the dial that
    would tell you where it points does not exist.

    ``needs_human`` is reported as its RESOLVED number rather than as the stored ``0``,
    because "unset" here does not mean "no window" — ``store.sweep_stale`` derives it from
    the working threshold by a multiplier. Showing the raw config value would tell an
    operator a question is never released, which is the opposite of what happens.

    ``needs_human_derived`` keeps that honest: it says whether the number above is the
    operator's own or one we computed for them, so the UI can offer "back to default"
    without pretending the two cases are the same.
    """
    # Imported here, not at module scope: ``dispatch`` imports this module's ``resolve_mode``
    # and ``describe``, so a top-level import would close a cycle. The defaults live beside
    # the code that applies them, and duplicating them here is exactly the drift this
    # function exists to close.
    from kiro_crew.apps.builtins.ops_mission_control.backend.dispatch import (
        DEFAULT_MAX_CLAIMS_PER_CYCLE,
        DEFAULT_STALE_AFTER_SECS,
        _config_int,
    )
    from kiro_crew.apps.builtins.ops_mission_control.backend.store import (
        DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER,
    )

    stale_after = _config_int(_CONFIG_STALE_AFTER, DEFAULT_STALE_AFTER_SECS)
    needs_human = _config_int(_CONFIG_NEEDS_HUMAN_STALE_AFTER, 0)
    return {
        "max_claims_per_cycle": _config_int(_CONFIG_MAX_CLAIMS, DEFAULT_MAX_CLAIMS_PER_CYCLE),
        "stale_after_secs": stale_after,
        "needs_human_stale_after_secs": (
            needs_human or stale_after * DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER
        ),
        "needs_human_derived": not needs_human,
    }


def resolve_mode(signal: Signal) -> str:
    """Effective operating mode for one signal — tightest-wins.

    With no matching rule the app mode applies. A matching rule can only NARROW
    it, so a rule cannot escalate an instance the operator pinned to ``observe``.

    When SEVERAL rules match, the TIGHTEST one wins. This used to take the most
    permissive (``max``), which broke the one thing rules are for: adding a narrow
    ``observe`` rule to carve an exception out of a broad ``act`` grant did nothing,
    because the broad grant still won and the write was authorized. Carving out the
    exception is the whole reason to write the second rule, so the narrower rule has
    to be the one that decides. ``min`` also matches the algebra the rest of the
    module already uses -- ``effective_mode`` is ``min``, the governance ceiling is
    ``min`` -- so overlap now resolves the same direction everywhere. Found in review.
    """
    base = app_mode()
    matching = [r for r in load_rules() if r.matches(signal)]
    if not matching:
        return base
    # Tightest matching rule, then clamped again by the app ceiling.
    best = min(matching, key=lambda r: MODE_ORDER.get(r.mode, 0))
    return effective_mode(base, best.mode)


def _definitely_off_shift() -> bool:
    """True only when the rotation POSITIVELY says this instance is not on call.

    Synchronous, because ``authorize_action`` is called from a request handler that already
    holds the answer's inputs on disk — and because an await here would make every action
    authorization depend on a provider round trip.

    Consults EVERY configured real rotation source, not just the committed schedule file.
    Reading only the schedule meant a PagerDuty rotation reporting "someone else is on call"
    was invisible to the write gate: with no `rotation.yaml` on disk this returned False at the
    first line and `/incident/action` executed a production write against a provider the
    operator was not on call for. The rotation was consulted for TIER arming (via
    ``registry.resolve_shift``) and ignored for AUTHORIZATION — the one path where it matters
    most. Found in review.

    Mirrors ``resolve_shift``'s algebra deliberately: any real source reporting on-shift means
    on-shift (a person on two rotations is on call), ``is_fallback`` sources are skipped
    entirely (``AlwaysOnRotationSource`` is always configured and always on-shift, so counting
    it would make every real rotation unhearable — the same bug that already had to be fixed
    once in the registry), and this returns True only when at least one real source answered
    and none of them said on-shift.

    ``unknown`` is NOT off-shift: a missing or unparseable schedule, or an unreachable API,
    must not block an operator deliberately driving an action by hand. Returns False on any
    fault, so a broken rotation can never make the app unusable.

    Stays SYNCHRONOUS rather than awaiting ``registry.resolve_shift``: ``authorize_action`` is
    itself sync by design and its one caller already dispatches it through
    ``asyncio.to_thread``, so each source's sync core runs off the event loop. Going async here
    would push the await up through the whole gate for no gain.
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry

        sources = [
            src
            for src in get_registry().rotation_sources()
            if not getattr(src, "is_fallback", False)
        ]
        answered = False
        faulted = False
        for src in sources:
            try:
                # NOTE: deliberately NOT gated on `src.configured()`.
                #
                # That predicate reads `providers.<id>.enabled` out of `data/config.json` for
                # every adapter except the schedule file — and that file is agent-writable and
                # served unauthenticated. So a single flag flip made a rotation source report
                # unconfigured, this loop SKIPPED it, no real source answered, and the
                # off-shift refusal silently stopped firing. Reproduced: PagerDuty reporting
                # `on_shift=False` refused the write; setting `enabled: false` — changing
                # nothing about who is actually on call — returned "granted by rule on
                # cloudwatch" for the same signal.
                #
                # Asking every source instead is strictly safer AND simpler, because each one
                # already reports its own inability to answer as `unknown`, which the check
                # below treats as a non-vote: a schedule file that is absent or unparseable, a
                # PagerDuty with no `schedule_ids`, a missing secret (the request raises and
                # lands in the `except` below). Genuine indeterminacy still permits the action
                # — that is the documented design, so a broken rotation cannot lock an operator
                # out — but the agent can no longer MANUFACTURE indeterminacy from config.
                #
                # Fourth instance of one class this round: a security refusal must not depend
                # on an input the constrained party can write. The other three were the
                # rotation login, strict gating, and `config_fields` still advertising the
                # login to the generic provider-config route.
                status = _shift_sync(src)
            except Exception:  # noqa: BLE001 — one bad source must not decide the vote
                logger.warning(
                    "ops-mission-control: rotation source %r FAULTED; treating this instance "
                    "as off shift because a source that should be able to answer could not",
                    getattr(src, "id", src),
                    exc_info=True,
                )
                # A FAULT IS NOT AN ABSENCE. Review pushed on this and was half right.
                #
                # A source that is configured and raising — PagerDuty timing out, a revoked
                # token, DNS failing — is a source that WOULD have answered. Counting that as
                # "no information" let an off-shift instance write during the exact window a
                # rotation API is down, which is also when a real incident is most likely.
                #
                # But the review's fix (deny whenever any source is unknown) would break the
                # documented property that a broken rotation must not lock the operator out: an
                # UNCONFIGURED source, an absent schedule file, or a PagerDuty with no
                # `schedule_ids` all legitimately report `unknown`, and on a solo install that
                # is the normal state. Denying there makes a missing config silently disable
                # every manual action.
                #
                # So the two cases are separated rather than merged: a RAISE is a positive
                # off-shift vote (the source exists and is failing), while `unknown` stays a
                # non-vote (the source has nothing to say). `faulted` is tracked separately
                # from `answered` so a fault cannot be masked by another source's `unknown`.
                faulted = True
                continue
            if status is None:
                continue
            if status.unknown:
                # Cannot tell. Not an off-shift vote, and not an answer either.
                continue
            answered = True
            if status.on_shift:
                return False  # on call somewhere — allowed
        # A positive on-shift answer already returned False above, so reaching here means no
        # source said "you are on call". Refuse if any real source ANSWERED off-shift, and also
        # if one FAULTED — in both cases something that could speak to the question did not
        # clear this instance.
        return answered or faulted
    except Exception:  # noqa: BLE001 — never let a rotation fault block the operator
        logger.debug("ops-mission-control: off-shift check unavailable", exc_info=True)
        return False


def _shift_sync(src: Any) -> ShiftStatus | None:
    """One rotation source's shift status, WITHOUT an event loop.

    Both shipped sources compute their answer synchronously and wrap that core in an async
    ``on_shift()`` (`schedule_file.resolve_now`, `pagerduty._on_shift_sync`), so the sync core
    is preferred: it is the cheap path and it keeps this gate off the loop.

    **A source that offers only the coroutine is still ASKED.** ``async on_shift()`` is the
    entire public ``RotationSource`` contract — the two sync cores are private details of our
    own adapters — so abstaining on "no private method found" made the off-shift refusal
    silently inapplicable to any correctly-written companion source: it reported off shift,
    the vote counted it as not answering, and a matching act-rule then executed a real
    provider write from an off-shift instance. That is the same shape as the four earlier
    holes in this refusal (forge the identity, disable strict gating, hide behind
    ``configured()``), reached this time by implementing the documented interface. Found in
    review (GPT 5.6).

    ``asyncio.run`` is safe HERE specifically: every caller reaches this through
    ``authorize_action``, which routes run in a worker thread via ``asyncio.to_thread`` (see
    ``routes._authorize``) precisely because this path can spawn a blocking ``gh api user``.
    A worker thread has no running loop, so there is none to re-enter or block. The guard
    below is not decoration — if a loop IS running on this thread the call is on the loop by
    mistake, and the honest answer is to abstain rather than raise inside a security gate or
    freeze the gateway.
    """
    for name in ("_on_shift_sync", "resolve_now"):
        fn = getattr(src, name, None)
        if callable(fn):
            result = fn()
            return result if isinstance(result, ShiftStatus) else None
    on_shift = getattr(src, "on_shift", None)
    if not callable(on_shift):
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # No loop on this thread — the expected case; drive the coroutine below.
    else:
        logger.warning(
            "ops-mission-control: rotation source %r offers only async on_shift() and this "
            "gate is running ON the event loop, so it cannot be consulted; treating it as "
            "unable to answer",
            getattr(src, "id", src),
        )
        return None
    coro = on_shift()
    if not inspect.isawaitable(coro):
        return coro if isinstance(coro, ShiftStatus) else None
    # Exceptions PROPAGATE, deliberately. The caller's per-source `except` records a raise as
    # `faulted`, which is a positive off-shift vote — a configured source that is failing is
    # one that WOULD have answered. Swallowing it here would downgrade that fault to an
    # absence and permit the write during exactly the window a rotation provider is down.
    # A timeout raises `asyncio.TimeoutError`, which is such a fault and lands the same way.
    #
    # An explicit loop rather than `asyncio.run`: same semantics on a thread that owns no
    # loop, but `asyncio.run` reads as `<base>.run` to the spawn-audit tripwire
    # (`test_spawn_audit.py` matches `asyncio`/`subprocess` × `run`/`Popen`/…), so it would
    # register this function as an unrouted SUBPROCESS spawn. Tripping a security tripwire to
    # save a line is a bad trade — and adding this to that test's benign allowlist would be
    # worse, since the allowlist should carry real spawns that are known-safe, not false
    # positives that quietly raise the noise floor for the next reader.
    loop = asyncio.new_event_loop()
    try:
        return _as_shift(loop.run_until_complete(_await_shift(coro)))
    finally:
        try:
            loop.close()
        finally:
            # `new_event_loop()` does not install itself, but a library the companion calls
            # may have set one on this thread; clearing keeps the worker thread as we found it.
            asyncio.set_event_loop(None)


def _as_shift(value: Any) -> ShiftStatus | None:
    """A ``ShiftStatus`` or ``None`` — never a foreign object from a companion source."""
    return value if isinstance(value, ShiftStatus) else None


async def _await_shift(coro: Any) -> Any:
    """Await one source's ``on_shift()`` under a bounded timeout.

    Bounded because a companion source is a network client we do not control and this gate
    sits in front of an operator action, so a hung provider must not hold the request open.
    The timeout is a FAULT, not a shrug: it propagates so the caller counts it as off-shift
    (see ``_shift_sync``), because a source that hangs is a source that could not clear this
    instance.
    """
    return await asyncio.wait_for(coro, timeout=_ASYNC_SHIFT_TIMEOUT_SECS)


def authorize_action(signal: Signal, action: str) -> tuple[bool, str]:
    """Decide whether ``action`` may actually execute against ``signal``.

    Returns ``(allowed, reason)``. Every decision — allow and deny — is
    SEL-audited, because this is the boundary where the agent gains the ability
    to change something in the user's production tooling.
    """
    if action not in VALID_ACTIONS:
        return _audited(signal, action, False, f"unknown action {action!r}")

    # An OFF-SHIFT instance may not write to a provider, even with `act` mode and a
    # matching rule.
    #
    # The tier gate only pauses SCHEDULED work. This path is reachable independently — the
    # `/incident/action` route, and an investigation already in flight when a shift ends —
    # so an off-shift teammate could acknowledge or resolve a real page in the operator's
    # production tooling. Verified before fixing: bob off shift, `dispatch` tier disarmed,
    # and `authorize_action` still returned "granted by rule on cloudwatch".
    #
    # Same lesson as the two bugs before it: a gate one layer up does not protect a path
    # that does not pass through it. Arming decides WHEN we look; this decides whether we
    # may act, and both have to be checked where they are enforced.
    #
    # A solo install is unaffected: with no rotation source, `resolve_shift` reports
    # `unknown=True` and this only refuses a DEFINITE off-shift answer — the same
    # distinction strict gating draws. Deliberately narrow: refuse when the rotation
    # positively says someone else owns the shift, never when it merely cannot tell.
    off_shift = _definitely_off_shift()
    if off_shift:
        return _audited(
            signal,
            action,
            False,
            "this instance is off shift — the on-call instance owns provider writes",
        )

    mode = resolve_mode(signal)
    if MODE_ORDER.get(mode, 0) < MODE_ORDER[MODE_ACT]:
        return _audited(
            signal,
            action,
            False,
            f"mode is {mode!r} — execution requires 'act'",
        )

    matching = [r for r in load_rules() if r.matches(signal) and r.mode == MODE_ACT]
    if not matching:
        return _audited(signal, action, False, "no matching act-rule for this signal")

    # A rule may narrow which actions it grants. An empty set means "any action
    # this sink supports", which is the common case for a tightly-scoped rule.
    for rule in matching:
        if not rule.actions or action in rule.actions:
            return _audited(signal, action, True, f"granted by rule on {rule.source}")
    return _audited(signal, action, False, f"matching rule does not grant {action!r}")


def _audited(signal: Signal, action: str, allowed: bool, reason: str) -> tuple[bool, str]:
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="action_authorize",
        outcome="success" if allowed else "rejected",
        resources=f"signal={signal.id} action={action}",
        error="" if allowed else reason,
    )
    return allowed, reason


def is_primary() -> bool:
    """Whether this instance runs the ``primary`` tier (ledger hygiene).

    **The committed schedule's ``leader`` wins when it names one.** Local config decides
    only when the shared file is silent.

    Why: ``primary_instance`` defaults to ``True`` and lives in each instance's own
    config, so on a team where nobody opted out, EVERY instance claimed the primary tier —
    verified with three default installs, all reporting ``is_primary=True``. That means N
    agents concurrently running dedupe/decay/**prune** against one shared ledger, which is
    the same shape as the double-claim the shared schedule exists to prevent, just on the
    maintenance path instead of the incident path. Concurrent prunes are worse than
    concurrent claims: a claim wastes a turn, a prune deletes knowledge.

    The natural fix is a ``leader:`` field in the shared team file, and
    that is the right shape — one fact, in the file everyone already reads, rather than N
    local settings that must agree by convention. `primary_instance` stays honoured so a
    solo install and an explicitly-configured team both keep working.

    The local flag is read from the OPERATOR-ONLY keystone, never ``config.json``: it is the
    no-identity path to the ledger-prune gate, so leaving it agent-writable would let an agent
    self-promote past the 409 and prune a shared ledger it does not own. See
    ``policy_store.PRIMARY_KEY``.
    """
    leader = _schedule_leader()
    if leader:
        me = _schedule_me()
        # No resolvable login means this instance cannot prove it is the leader. Answer
        # False: a missed nightly hygiene pass is recoverable on the next run, whereas
        # every instance pruning the shared ledger is not.
        return bool(me) and me.lower() == leader.lower()
    return bool(policy_store.get(policy_store.PRIMARY_KEY, True))


def primary_owner() -> str:
    """Who owns the ``primary`` tier per the shared schedule, or "" when unnamed.

    Public because a refusal has to be able to say WHO instead of just "not you". An
    operator told only "this instance is not the primary" has no next step; told the
    leader's name, they either know it is correct or know the schedule is wrong.

    Returns "" when the schedule names no leader — in which case ``is_primary`` falls back
    to local ``primary_instance`` config and there is no shared answer to report.
    """
    return _schedule_leader()


def _schedule_leader() -> str:
    """The ``leader:`` named in the committed schedule, or "". Never raises."""
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            schedule_file,
        )

        return schedule_file.leader()
    except Exception:  # noqa: BLE001 — a broken schedule must not break tier arming
        logger.debug("ops-mission-control: could not read the schedule leader", exc_info=True)
        return ""


def _schedule_me() -> str:
    """This instance's resolved GitHub login, or "". Never raises."""
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            schedule_file,
        )

        return schedule_file.resolve_login()
    except Exception:  # noqa: BLE001
        logger.debug("ops-mission-control: could not resolve this instance's login", exc_info=True)
        return ""


def tier_states(shift: ShiftStatus) -> dict[str, bool]:
    """Which tiers should be armed, given the current shift status.

    **``on_shift`` alone decides.** ``unknown`` is an explanation for the UI, never an
    arming input.

    This used to read ``shift.on_shift or shift.unknown``, which silently defeated strict
    gating for exactly the case it was written for: a schedule that cannot say whether this
    operator is on call returns ``on_shift=False, unknown=True``, and the ``or`` re-armed
    it anyway. Verified before fixing — an instance with no resolvable login reported
    ``on_shift=False`` and ``dispatch armed=True``, so every teammate would still pick up
    the same alarm.

    The fail-open intent is still available and now lives where it belongs: each
    ``RotationSource`` decides what "cannot tell" means for it. A rotation *API* returns
    ``on_shift=True, unknown=True`` (a network fault must not disable response); the
    committed schedule returns ``on_shift=False, unknown=True`` under ``strict_gating``.
    Two sources, two policies, one gate that just reads the answer.
    """
    on_shift_armed = bool(shift.on_shift)
    return {
        TIER_ALWAYS: True,
        TIER_ON_SHIFT: on_shift_armed,
        TIER_PRIMARY: is_primary(),
    }


def crons_for_tier(tier: str) -> tuple[str, ...]:
    return TIER_CRONS.get(tier, ())


#: Cron names this app must never allow to be paused, whatever asks. Derived from the tier
#: table rather than restated, so a job that moves onto ``always`` is protected by that move
#: alone and cannot be protected in one place and forgotten in the other.
def protected_cron_names() -> frozenset[str]:
    """The always-tier jobs — the only ones that can re-arm a gated instance."""
    return frozenset(crons_for_tier(TIER_ALWAYS))


async def apply_tiers(shift: ShiftStatus, cron_service: Any) -> dict[str, Any]:
    """Arm/disarm this app's crons to match the tier map, server-side.

    Why this exists rather than the agent issuing ``cron_pause``/``cron_resume``:

    tier arming used to be entirely the ``rotation-check`` agent's job, and the ONLY thing
    stopping it pausing ``rotation-check`` itself — the sole always-tier job, and so the only
    job that can ever re-arm a gated instance — was one sentence of SOP prose telling it not
    to. A single misfollowed turn silently ends incident response until a human notices,
    which is precisely the quiet-versus-broken conflation this app refuses everywhere else
    ("a source that failed is shown as failed, never as quiet"). Prose is not an enforcement
    mechanism. Found in design review.

    So the whole arming decision moves here, into deterministic code the model does not
    mediate: the caller is a route, the tier map is computed from the shift, and
    ``protected_cron_names()`` is skipped unconditionally — this function cannot pause an
    always-tier job even if the tier map somehow said to. The agent's remaining role is to
    POST, which is why the SOP no longer needs it to hold ``cron_pause`` at all.

    ``cron_service`` is duck-typed (``list_jobs_async`` + ``enable_job_async``) so tests pass
    a fake. Returns a summary of what changed, so a caller can stay silent when nothing did.
    """
    if cron_service is None:
        return {"ok": False, "code": "cron_service_unavailable", "changed": []}

    # Off the loop, same reason as `dispatch.run_cycle`: `tier_states` can reach a `gh`
    # subprocess, and `apply_tiers` is awaited from `POST /rotation/arm` (the default-enabled
    # 300s rotation-check cron). Found in review.
    states = await asyncio.to_thread(tier_states, shift)
    protected = protected_cron_names()
    # ONLY the `on_shift` tier is armed from here, which is exactly the scope the agent
    # had. Deliberately not widened to every tier while fixing the self-disarm hole:
    #
    # - `always` must never be paused at all — that is the whole point (see `protected`).
    # - `primary` (`ledger-hygiene`) ships ENABLED and is gated in the ROUTE instead:
    #   `POST /ledger/hygiene` refuses with 409 `not_primary`. Pausing it here would stop
    #   the 03:17 job on every non-primary instance — a real behavior change, not a
    #   security fix, and `is_primary()` can transiently answer False (it shells out to
    #   `gh api user`), so arming on it would make a network blip silently skip a night's
    #   maintenance. If the primary tier should arm its cron too, that is its own change
    #   with its own measurement.
    desired: dict[str, bool] = {
        cron_name: states[TIER_ON_SHIFT] for cron_name in crons_for_tier(TIER_ON_SHIFT)
    }

    # `list_jobs_async`, NOT `list_jobs` wrapped in `to_thread`. `list_jobs` is documented
    # cache-only (no file I/O, safe on the loop) but reads an in-memory snapshot that is only
    # refreshed within one timer poll — so a pause the operator just made from the CLI or the
    # dashboard could still look active here and we would "resume" a job nobody wanted armed.
    # `list_jobs_async` does the locked `_sync()` in a worker: fresh AND off-loop.
    jobs = await cron_service.list_jobs_async(True)
    changed: list[dict[str, Any]] = []
    for job in jobs:
        name = str(getattr(job, "name", ""))
        if name not in desired:
            continue
        want = desired[name]
        if not want and name in protected:
            # Unreachable via `tier_states` (always is hardcoded True) and deliberately
            # still checked: this is the invariant, not a consequence of one caller.
            logger.warning("ops-mission-control: refusing to pause protected cron %s", name)
            continue
        if bool(getattr(job, "enabled", False)) == want:
            continue
        await cron_service.enable_job_async(getattr(job, "id", ""), enabled=want)
        changed.append({"name": name, "enabled": want})

    if changed:
        sel().log_api_access(
            caller="ops-mission-control",
            operation="rotation.apply_tiers",
            outcome="allowed",
            resources=" ".join(f"{c['name']}={'on' if c['enabled'] else 'off'}" for c in changed),
        )
    return {"ok": True, "changed": changed, "tiers": states}


def rule_to_dict(rule: AutonomyRule) -> dict[str, Any]:
    """Serialize a parsed rule for the dashboard, in the shape ``PUT /settings`` accepts.

    Round-trippable on purpose: the Settings editor reads `rules_detail`, edits, and PUTs it
    straight back, so the read and write shapes must be the same object.
    """
    entry: dict[str, Any] = {"source": rule.source, "mode": rule.mode}
    if rule.resource_glob:
        entry["resource_glob"] = rule.resource_glob
    if rule.label_match:
        entry["label_match"] = dict(rule.label_match)
    if rule.actions:
        entry["actions"] = sorted(rule.actions)
    return entry


def describe(shift: ShiftStatus) -> dict[str, Any]:
    """Rotation + autonomy summary for the dashboard."""
    states = tier_states(shift)
    rule_dicts = [rule_to_dict(rule) for rule in load_rules()]
    return {
        "on_shift": shift.on_shift,
        "who": shift.who,
        "until": shift.until,
        "unknown": shift.unknown,
        "tiers": states,
        # Flat union across all ARMED tiers — what is running right now.
        "armed_crons": sorted(
            name for tier, armed in states.items() if armed for name in crons_for_tier(tier)
        ),
        # Per-tier breakdown, which is what the rotation-check SOP actually needs.
        # Without it the only cron list on this response is the flat union above,
        # which OFF shift still contains ``ops-mission-control/rotation-check`` (an
        # ``always``-tier job). An agent told to "pause the armed crons" would then
        # pause the very cron that re-arms the instance, permanently disabling
        # incident response. The SOP must pause exactly ``tier_crons.on_shift``.
        "tier_crons": {tier: list(crons_for_tier(tier)) for tier in states},
        "mode": app_mode(),
        "rules": len(rule_dicts),
        # The rules THEMSELVES, not just how many. A count cannot be rendered, edited or
        # verified: an operator could not see which grants existed, and the Settings panel
        # had nothing to populate an editor from. Serialized from the PARSED rules, so what
        # the operator sees is what the gate will actually use — an entry that failed
        # validation is absent here rather than displayed as if it were live.
        "rules_detail": rule_dicts,
        "primary": is_primary(),
        "modes_available": [MODE_OBSERVE, MODE_PROPOSE, MODE_ACT],
        # The whole team, when a committed schedule is the rotation source. `who` alone
        # cannot tell an operator whether this instance is idle because a teammate holds
        # the pager or because the file is broken — and a silently-idle instance is the
        # failure mode a shared schedule introduces. Empty dict when no schedule is in
        # use, so the UI simply renders nothing rather than an empty team.
        "roster": _roster_safely(),
        # How fast the heartbeat claims, and how long it lets work sit before releasing it.
        # On this response because it is already the payload Settings reads for `mode` and
        # `primary` — the two other write-only-until-now knobs — and adding a route for
        # three integers would buy nothing.
        "sweep": sweep_windows(),
        # The two FENCED rotation identities, so Settings can render and edit them.
        #
        # They have to be reported here rather than through `GET /providers`: both moved off
        # `config_fields` onto the keystone floor (they are inputs to the off-shift refusal, and
        # provider config is agent-writable), so the provider catalog no longer carries them and
        # the generic field renderer cannot see them. Reporting the VALUE is fine — an identity
        # is not a credential, `roster.me` already publishes the resolved login, and an operator
        # who cannot see which identity is stored cannot tell a wrong one from an unset one.
        "identities": {
            "schedule_github_login": str(policy_store.get(policy_store.SCHEDULE_LOGIN_KEY) or ""),
            "pagerduty_user_id": str(policy_store.get(policy_store.PAGERDUTY_USER_KEY) or ""),
        },
    }


def _roster_safely() -> dict[str, Any]:
    """The schedule-file roster, or ``{}``. Never raises.

    Read through a guarded call because ``describe`` backs the dashboard's main poll: a
    malformed schedule a teammate pushed must not 500 the board. The rotation ITSELF
    already degrades safely; this protects the display path too.
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            schedule_file,
        )

        if not schedule_file.schedule_path().exists():
            return {}
        return schedule_file.roster()
    except Exception:  # noqa: BLE001 — a display extra must never break the board
        logger.debug("ops-mission-control: roster unavailable", exc_info=True)
        return {}
