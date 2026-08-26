"""Interrupt controller for agent sessions: cheap polling, expensive wakes.

A model turn is the expensive execution context here, the way a CPU is in an
operating system. Having it poll — waking every interval to ask "anything
new?" and answer "no" — is the wasteful arrangement OS designers abandoned
decades ago. The alternative is an interrupt: something cheap watches the
device and raises a line only when there is genuinely something to service.

That is what this module is. A script cron plays the device controller: it
observes an external subject on a schedule, costs no model call at all while
nothing is happening, and only an unexpected observation raises a wake — the
agent turn that gets scheduled is the interrupt service routine.

The vocabulary is deliberately the OS one, because every design question this
mechanism raises already has a well-worn answer under that name:

===========================  ==================================================
Interrupt concept            Here
===========================  ==================================================
Interrupt source             a :class:`Probe`, polled once per cron tick
ISR                          the agent turn the gateway schedules on a wake
Masking                      time-bounded dedupe, so one condition wakes once
Coalescing                   several anomalies folded into a single wake
NMI                          :attr:`Severity.NMI` — never delayed by coalescing
Clearing a pending bit       epoch reset, when the subject becomes another one
Stuck / spurious IRQ         the consecutive-error backstop
Unregistering an IRQ line    :attr:`Severity.TERMINAL` — the job removes itself
===========================  ==================================================

The split of work follows Linux's top half / bottom half: the probe is the top
half (must be fast and cheap, decides only *whether* something happened), and
the woken agent turn is the bottom half (does the real work, may be slow).

**Why coalescing is here, and why not for the reason a NIC driver has it.** A
network card coalesces because interrupts are microseconds each but arrive
tens of thousands per second, so the volume buries the CPU. This module's
frequency is the opposite — minutes apart — so "too many" is not the problem.
Two other things are:

1. **A wake raised before the subject settles cannot be serviced.** Waking an
   agent about one failing check while twenty-four others are still running
   produces a turn that structurally cannot decide anything: it does not yet
   know whether more failures are coming or whether they share a cause. That
   turn has no output regardless of what it costs.
2. **The follow-up action is usually shared.** Two failing checks on one pull
   request are fixed by one edit and one push. Servicing them separately means
   two pushes and two full CI rounds — the waste is wall-clock and CI
   capacity, not tokens.

So the rule for whether to coalesce is *whether servicing the signals shares
an action*, not how many there are. Signals on different subjects never
coalesce here, because state is keyed per subject and per cron job.

Coalescing is not free: a window cannot open and fire within one tick, so it
costs at least one cron interval of added latency. ``coalesce_secs=0`` turns
it off for callers that would rather be woken early than woken once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.cron_script import Done, Report, Skip

logger = logging.getLogger(__name__)

#: The probe-authoring surface. Deliberately minimal, because advertising a
#: name is a compatibility promise: everything here has a real consumer, and
#: everything else -- `load_state`, `save_state`, `state_path`, the non-default
#: tuning constants -- is reachable but unexported, since its only callers are
#: this module and the tests that inspect a watch's persisted state.
__all__ = [
    "Observation",
    "Probe",
    "Severity",
    "Tick",
    "run",
    "sanitize_label",
    "DEFAULT_COALESCE_SECS",
]

#: A fired alert re-arms after this long while its condition persists. The
#: script cannot observe delivery -- it raises Report and exits, and the
#: gateway delivers afterwards -- so dedupe is a bounded delay rather than a
#: permanent acknowledgement. A permanent marker turns one lost delivery into
#: a permanently suppressed signal.
DEFAULT_REALERT_SECS = 6 * 3600

#: Consecutive failed observations before the kernel reports that the watch
#: is blind.
DEFAULT_MAX_CONSECUTIVE_ERRORS = 6

#: Shortest a coalescing window stays open. This is a floor, not a timeout: right
#: after a subject changes epoch its sub-observations may not exist yet (a
#: freshly pushed commit has an almost-empty check rollup), so ``pending == 0``
#: can be briefly true while nothing has actually run. Firing on that reports
#: a converged state that never happened.
DEFAULT_COALESCE_SECS = 240.0

#: Hard wall-clock bound on a coalescing window, measured from the FIRST anomaly.
#: Reached only when ``pending`` never drains (a check wedged in queued, or a
#: phantom pending row). Independent of ``pending`` on purpose: it is what
#: guarantees a delayed wake rather than a lost one.
DEFAULT_COALESCE_MAX_SECS = 1800.0

#: Cap on how many observation labels a coalesced wake spells out.
_MAX_LIST = 8

_SAFE_LABEL_RE = re.compile(r"[^\w .,:()\[\]/+#-]")
_FOLD_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_label(value: object) -> str:
    """Fold externally-authored text for use in state keys and wake briefs.

    Observation labels are attacker-influenceable (a CI workflow names its
    own jobs, a ticket title is user input) and a wake brief is injected
    into an agent turn, so strip anything that could smuggle markup or
    control characters. Non-strings fold to empty rather than raising: a
    malformed row must cost one observation, never the tick.
    """
    if not isinstance(value, str):
        return ""
    return _SAFE_LABEL_RE.sub("_", value)[:120]


class Severity(Enum):
    """How the kernel treats one observation."""

    #: An anomaly. Masked, and folded into a coalesced wake.
    WAKE = 1
    #: The subject reached an end state: deliver and remove the cron job.
    TERMINAL = 2
    #: An anomaly that BYPASSES the coalescing window and fires now. Reserved for
    #: conditions under which waiting observes nothing further -- a merge
    #: conflict dispatches no checks, so ``pending`` will never drain and the
    #: delay would strand the operator for the full hard cap on a signal that
    #: is already actionable.
    #:
    #: It bypasses the DELAY, not the mask. A persisting condition still wakes
    #: at most once per re-alert window, because the alternative -- an unmasked
    #: NMI -- would wake the operator on every single tick for as long as the
    #: condition lasts, which is a worse failure than a bounded delay.
    NMI = 3


@dataclass(frozen=True)
class Observation:
    """One thing a probe saw during one tick.

    A probe reports only what it wants the kernel to act on. There is
    deliberately no "seen and fine" severity: an observation the kernel would
    neither wake nor terminate on is simply not returned. An earlier revision
    carried one, plus an ``expected`` flag whose recorded state nothing read --
    a write with no reader, which is the shape of state that rots.

    Attributes:
        key: Stable dedupe identity *within an epoch*. Two ticks reporting
            the same key describe the same anomaly, and the kernel fires it
            at most once per re-alert window.
        severity: See :class:`Severity`.
        brief: Operator-facing text delivered if this observation wakes.
    """

    key: str
    severity: Severity
    brief: str = ""


@dataclass
class Tick:
    """A probe's complete report for one tick.

    Attributes:
        epoch: Identity token of the subject as observed *this* tick. When it
            differs from the persisted epoch the kernel wipes all dedupe
            memory before evaluating. Empty means "this subject has no
            identity token", which disables epoch resets for it.
        observations: Everything seen this tick.
        pending: Count of sub-observations not yet settled. Drives the coalescing
            window's convergence close.
        fetch_ok: False when the probe could not observe the subject at all.
            Feeds the error backstop; ``observations`` is ignored.
        detail: Optional one-line reason echoed into the kernel's ``Skip``
            message, for cron-history readability.
    """

    epoch: str = ""
    observations: list[Observation] = field(default_factory=list)
    pending: int = 0
    fetch_ok: bool = True
    detail: str = ""


class Probe:
    """Domain half of a watch. Subclass and implement both methods.

    A probe must NOT raise :class:`~kiro_crew.cron_script.Skip`,
    ``Report`` or ``Done`` -- those are the kernel's verdict, and a probe
    raising them re-decides the policy the kernel exists to own.
    """

    def identity(self, ctx: object) -> tuple[str, str]:
        """Return ``(subject_kind, subject_id)`` and parse the cron message.

        ``subject_kind`` groups a family of watches (``"gh-pr"``) and
        ``subject_id`` names one subject within it (``"owner/name#123"``).
        Together with the cron job id they form the state identity, so two
        cron jobs watching one subject never share a dedupe memory.

        Validate ``ctx.message`` here and raise :class:`ValueError` for a
        configuration that can never become valid -- the kernel converts
        that to ``Done``, because a malformed parameter cannot self-heal and
        retrying it forever is a crash loop with extra steps.
        """
        raise NotImplementedError

    def observe(self, ctx: object) -> Tick:
        """Perform ONE bounded observation and classify what was seen.

        Must not raise for an expected failure: return
        ``Tick(fetch_ok=False)`` and let the kernel own the error backstop.
        """
        raise NotImplementedError

    def tuning(self) -> dict[str, float]:
        """Optional overrides for the kernel's bounds, by keyword name.

        Recognized key: ``coalesce_secs``. Unknown keys are ignored.

        Only the one key any probe actually produces is accepted. The kernel has
        three other bounds and the mechanism here is generic over the mapping, so
        admitting all four would cost nothing mechanically -- but a recognized
        key with no producer is a contract nobody has exercised, and the second
        probe would build on a shape that was never tested. Re-admit a name when
        a probe produces it.

        A value the kernel cannot use (negative, non-finite, or too large to
        represent as a float) is refused in favour of the default rather than
        allowed to raise on every tick.

        Called after :meth:`identity`, so a probe may derive an override from
        its own cron message -- which is why this exists as a declared method
        rather than the kernel reading an attribute off the probe. An implicit
        attribute back-channel is a second, undocumented way to configure the
        kernel, and the second probe would have copied it.
        """
        return {}


def _state_dir() -> Path:
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else Path.home() / ".kiro" / "crew"
    return base / "watch"


def state_path(subject_kind: str, subject_id: str, job_id: str) -> Path:
    """Per-WATCH state file, never shared between cron jobs.

    The job id is part of the digest so two watches on one subject keep
    independent alert memories: one watch's dedupe must not suppress the
    other's delivery. The digest covers the exact, unfolded subject id so
    that two ids which fold to the same characters cannot collide into one
    file while the human-readable prefix stays readable.
    """
    kind = _FOLD_RE.sub("_", subject_kind)[:40] or "watch"
    fold = _FOLD_RE.sub("_", subject_id)[:60]
    digest = hashlib.sha256(f"{subject_kind}#{subject_id}#{job_id}".encode("utf-8")).hexdigest()[
        :10
    ]
    return _state_dir() / kind / f"{fold}-{digest}.json"


def _coerce_ts(value: object) -> float | None:
    """A finite float timestamp, or None when the stored value is unusable.

    ``json.loads`` yields arbitrary-precision ints and accepts ``Infinity`` /
    ``NaN`` literals. A 4000-digit timestamp raises OverflowError on float()
    and a NaN silently poisons every dedupe comparison, so both must drop the
    entry -- costing one duplicate wake -- rather than crash the tick into the
    cron auto-pause path.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except OverflowError:
        return None
    return ts if math.isfinite(ts) else None


def load_state(path: Path) -> dict:
    """Read state, coercing every field to its expected type.

    Malformed persisted state -- hand-edited, truncated, or written by a
    different version -- must degrade to fresh state, which costs one
    duplicate wake, and never to a crash loop.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # ValueError subsumes JSONDecodeError and UnicodeDecodeError as well
        # as the bare ValueError CPython raises past the int-str conversion
        # limit; RecursionError covers pathologically deep nesting.
        return {}
    if not isinstance(data, dict):
        return {}
    state: dict = {}
    if isinstance(data.get("epoch"), str):
        state["epoch"] = data["epoch"]
    alerted = data.get("alerted")
    if isinstance(alerted, dict):
        kept: dict[str, float] = {}
        for key, raw in alerted.items():
            if not isinstance(key, str):
                continue
            ts = _coerce_ts(raw)
            if ts is not None:
                kept[key] = ts
        state["alerted"] = kept
    errors = data.get("errors")
    if isinstance(errors, int) and not isinstance(errors, bool) and errors >= 0:
        state["errors"] = errors
    started = _coerce_ts(data.get("coalesce_started_at"))
    if started is not None:
        state["coalesce_started_at"] = started
    window_rows = data.get("coalescing")
    if isinstance(window_rows, dict):
        pending_wakes: dict[str, str] = {}
        for key, brief in window_rows.items():
            if isinstance(key, str) and isinstance(brief, str):
                pending_wakes[key] = brief
        state["coalescing"] = pending_wakes
    return state


def save_state(path: Path, state: dict) -> bool:
    """Persist state. Returns False when the write failed.

    Uses the shared :func:`~kiro_crew.atomic_write.atomic_write`: a
    ``mkstemp`` temporary with an unpredictable name plus rename, so a
    pre-planted symlink at a guessable ``.tmp`` path cannot redirect it.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(state), mode=0o600)
        return True
    except OSError:
        return False


_PERSIST_WARNING = (
    "WARNING: the watch's state directory is unwritable, so alert "
    "deduplication is degraded (repeats possible). Fix permissions on the "
    "watch directory under the data home."
)


def _coalesced_brief(briefs: list[str]) -> str:
    """One wake body for several coalesced observations."""
    shown = briefs[:_MAX_LIST]
    if len(briefs) > len(shown):
        shown = shown + [f"(+{len(briefs) - len(shown)} more)"]
    return "\n\n".join(shown)


def _usable_bound(name: str, raw: object, default: float) -> float:
    """A bound the kernel can actually compute with, or *default*.

    One chokepoint for every numeric bound, because a value reaches ``run()``
    from three places -- its own arguments, a probe's ``tuning()``, and through
    either of those a cron message. ``json.loads`` yields three separately
    hostile shapes, and each one kills the cron the same way: by raising on
    every tick, which auto-pauses the job, so the watch dies silently from one
    bad number.

    * ``1e309`` parses to ``inf`` and reaches ``int()`` -> OverflowError.
    * A 401-digit integer is an arbitrary-precision ``int``; ``float()`` on it
      raises OverflowError.
    * ``NaN`` poisons every comparison silently, so the window neither holds
      nor fires.

    Refusing the value and keeping the default is the right degradation: a
    watch running on a sane bound beats no watch at all.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        logger.warning("irq: %s is not a number (%r); using %r", name, raw, default)
        return default
    try:
        value = float(raw)
    except OverflowError:
        logger.warning("irq: %s is too large to represent (%r); using %r", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0:
        logger.warning("irq: %s is not a usable bound (%r); using %r", name, raw, default)
        return default
    return value


def run(
    ctx: object,
    probe: Probe,
    *,
    realert_secs: float = DEFAULT_REALERT_SECS,
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    coalesce_secs: float = DEFAULT_COALESCE_SECS,
    coalesce_max_secs: float = DEFAULT_COALESCE_MAX_SECS,
) -> None:
    """Run one tick of *probe* and raise the kernel's verdict.

    This is the only place ``Skip`` / ``Report`` / ``Done`` are raised.

    ``coalesce_secs=0`` disables the coalescing window, restoring fire-on-first-
    anomaly behaviour. That is the migration setting for an EXISTING poller
    moving onto the kernel: it ports with the window off, which is a pure
    structural change with no shift in wake timing, and enables the window as a
    separate, attributable step.

    The first probe is deliberately the exception, and it is worth naming so the
    rule does not read as violated. The coalescing window exists because of a
    defect measured on that probe, so porting it with the window off would ship
    a change that fixes nothing. The rule is about not bundling a timing change
    with an unrelated structural move; here the timing change IS the change.

    Coalescing semantics -- a window opens on the first non-exempt anomaly of the
    current epoch and fires when::

        elapsed >= coalesce_secs and (pending == 0 or elapsed >= coalesce_max_secs)

    ``coalesce_secs`` is a FLOOR rather than a timeout, which is the difference
    that matters: a subject that just changed epoch may report ``pending ==
    0`` before its sub-observations even exist, and firing on that reports a
    convergence that never happened. ``coalesce_max_secs`` is the wall-clock
    wall for a ``pending`` count that never drains, so the worst case is a
    delayed wake, never a dropped one.
    """
    try:
        subject_kind, subject_id = probe.identity(ctx)
    except ValueError as exc:
        raise Done(f"watch: {exc}; removing the watch") from exc

    # Per-probe tuning, read AFTER identity() so a probe may derive an override
    # from its own cron message (pr_watch derives coalesce_secs that way).
    # identity() is still called exactly once: calling it twice would parse the
    # message twice and, worse, put one call outside the ValueError-to-Done
    # conversion above.
    #
    # Every bound is validated here as well as wherever the probe validated it,
    # because a value can arrive from three places -- this call's arguments, a
    # probe's tuning(), and (through either) a cron message that json.loads may
    # have turned into inf, NaN, or an int too large for a float. Any of those
    # reaches int() in the Skip message below and raises on EVERY tick, and a
    # cron that raises every tick is auto-paused: the watch dies silently from
    # one bad number. Refusing the value and keeping the default is right,
    # because a watch running on a sane bound beats no watch.
    bounds: dict[str, float] = {
        "coalesce_secs": coalesce_secs,
        "coalesce_max_secs": coalesce_max_secs,
        "realert_secs": realert_secs,
        "max_consecutive_errors": max_consecutive_errors,
    }
    defaults: dict[str, float] = {
        "coalesce_secs": DEFAULT_COALESCE_SECS,
        "coalesce_max_secs": DEFAULT_COALESCE_MAX_SECS,
        "realert_secs": DEFAULT_REALERT_SECS,
        "max_consecutive_errors": DEFAULT_MAX_CONSECUTIVE_ERRORS,
    }
    try:
        overrides = probe.tuning() or {}
    except Exception:
        logger.warning("irq: probe tuning() raised; using defaults", exc_info=True)
        overrides = {}
    # Only the key a probe actually produces is honoured. The others stay
    # settable through this call's own arguments, which the tests use to drive
    # the error backstop and the hard cap on small bounds -- but a tuning() key
    # with no producer is an untested contract, so it is not recognized until
    # one exists.
    if "coalesce_secs" in overrides:
        bounds["coalesce_secs"] = overrides["coalesce_secs"]
    for name, raw in list(bounds.items()):
        bounds[name] = _usable_bound(name, raw, defaults[name])
    coalesce_secs = bounds["coalesce_secs"]
    coalesce_max_secs = bounds["coalesce_max_secs"]
    realert_secs = bounds["realert_secs"]
    max_consecutive_errors = int(bounds["max_consecutive_errors"])

    job = getattr(ctx, "job", None)
    job_id = str(getattr(job, "id", "") or "")
    path = state_path(subject_kind, subject_id, job_id)
    state = load_state(path)
    subject = f"{subject_kind} {subject_id}"
    persist_ok = True

    def persist() -> None:
        # Best-effort. An unwritable state directory must not remove the
        # watch (later signals would be lost) and must not silence it: the
        # watch keeps running, dedupe degrades to per-tick repeats, and every
        # wake carries a warning so the operator learns to fix the directory.
        nonlocal persist_ok
        if not save_state(path, state):
            persist_ok = False

    tick = probe.observe(ctx)

    if not tick.fetch_ok:
        errors = int(state.get("errors", 0)) + 1
        state["errors"] = errors
        persist()
        if not persist_ok:
            # The streak cannot be remembered, so the counted-threshold alert
            # below is unreachable and the watch would go silent forever. Say it
            # now, on EVERY such tick.
            #
            # The condition is deliberately not `errors == 1`. That form only
            # covers a directory that was already unwritable when the streak
            # began. If instead `errors: 1` persisted successfully and the
            # directory became unwritable afterwards, every later tick reloads
            # 1, reaches 2, fails to persist, and is neither == 1 nor >= the
            # threshold -- permanent silence from a state the watch itself
            # wrote. Repeats are the correct degradation here: dedupe needs the
            # same storage that just failed, so the alternative to repeating is
            # not repeating LESS, it is not alerting at all.
            raise Report(
                f"Watch on {subject}: the probe is failing AND the watch's "
                f"state directory is unwritable ({path.parent}), so the "
                "failure streak cannot be tracked. The watch is inoperative "
                "until both are fixed; expect this alert to repeat."
            )
        if errors >= max_consecutive_errors:
            # Fire at >= threshold with the same time-bounded re-arm as every
            # other alert. An exact-equality gate (fire only when errors ==
            # threshold) turns ONE lost delivery into permanent silence: the
            # persisted count passes the threshold and never equals it again.
            alerted = state.setdefault("alerted", {})
            blind_ts = _coerce_ts(alerted.get("blind"))
            now = time.time()
            # 0 <= elapsed: a future timestamp (clock rollback, corrupt
            # state) must read as stale, not suppress the alert forever.
            fresh = blind_ts is not None and 0 <= now - blind_ts < realert_secs
            if not fresh:
                alerted["blind"] = now
                persist()
                raise Report(
                    f"Watch on {subject}: the probe has failed {errors} "
                    "consecutive ticks (credentials expired? network?). The "
                    "watch is blind until this is fixed; it will re-alert "
                    "every few hours while the failure persists."
                )
            raise Skip(f"probe failed ({errors} consecutive; blind alert deduped)")
        raise Skip(f"probe failed ({errors} consecutive)")

    if state.get("errors"):
        state["errors"] = 0
        # A recovered streak clears the blind marker so the NEXT streak
        # alerts promptly instead of inheriting up to realert_secs of dedupe.
        state.get("alerted", {}).pop("blind", None)

    if tick.epoch and state.get("epoch") != tick.epoch:
        # The subject became a different subject: fresh epoch, fresh memory.
        # The in-flight coalescing window belongs to the old epoch and is dropped
        # with it -- its anomalies were observations of something that no
        # longer exists.
        state = {"epoch": tick.epoch, "alerted": {}, "errors": 0}

    alerted = state.setdefault("alerted", {})
    now = time.time()

    def should_alert(key: str) -> bool:
        ts = _coerce_ts(alerted.get(key))
        # 0 <= elapsed, as above: a future timestamp must read as stale.
        if ts is not None and 0 <= now - ts < realert_secs:
            return False
        return True

    def with_warning(body: str) -> str:
        return body if persist_ok else f"{body}\n\n{_PERSIST_WARNING}"

    terminal = [o for o in tick.observations if o.severity is Severity.TERMINAL]
    if terminal:
        persist()
        raise Done(with_warning(_coalesced_brief([o.brief for o in terminal if o.brief])))

    for obs in tick.observations:
        if obs.severity is Severity.NMI and should_alert(obs.key):
            alerted[obs.key] = now
            persist()
            raise Report(with_warning(obs.brief))

    fresh_wakes = {
        o.key: o.brief
        for o in tick.observations
        if o.severity is Severity.WAKE and should_alert(o.key)
    }

    if coalesce_secs <= 0:
        if fresh_wakes:
            for key in fresh_wakes:
                alerted[key] = now
            persist()
            raise Report(with_warning(_coalesced_brief(list(fresh_wakes.values()))))
        persist()
        raise Skip(tick.detail or f"{tick.pending} pending")

    # Prune before extending: an anomaly that CLEARED while the window was
    # open must not be reported. Without this an entry lives until the window
    # fires, so a check that reran green would still be announced as failing,
    # potentially in the same brief as the all-green observation that replaced
    # it -- a wake that contradicts itself, and the operator has no way to tell
    # which half is current.
    observed_wakes = {o.key for o in tick.observations if o.severity is Severity.WAKE}
    window: dict[str, str] = {
        key: brief
        for key, brief in (state.get("coalescing") or {}).items()
        if key in observed_wakes
    }
    window.update(fresh_wakes)
    if window:
        state["coalescing"] = window
        if not _coerce_ts(state.get("coalesce_started_at")):
            state["coalesce_started_at"] = now
        # Persist BEFORE evaluating the fire condition, so ``persist_ok`` below
        # reflects this tick's write rather than a stale initial value: whether
        # the window can be remembered is exactly what decides if delaying it is
        # safe.
        persist()
    else:
        # Everything in flight cleared. Close the window rather than leaving a
        # start stamp behind, or the NEXT anomaly would inherit this window's
        # age and could fire without any settling time of its own.
        state.pop("coalescing", None)
        state.pop("coalesce_started_at", None)
        persist()
        raise Skip(tick.detail or f"{tick.pending} pending")

    started = _coerce_ts(state.get("coalesce_started_at"))
    if started is None or started > now:
        # No start stamp, or one in the future (clock rollback): treat the
        # window as starting now rather than firing immediately or never.
        started = now
        state["coalesce_started_at"] = now
    elapsed = now - started
    converged = tick.pending == 0
    # The cap is an ABSOLUTE wall, so it must not be gated behind the floor.
    # Written as `elapsed >= floor and (converged or elapsed >= cap)` it was:
    # a caller passing a floor above the cap (legal, both finite and positive)
    # plus a pending count that never drains meant the cap could never be
    # reached first, and the guarantee it exists to make -- a delayed wake
    # rather than a dropped one -- was quietly void for those values.
    if (elapsed >= coalesce_secs and converged) or elapsed >= coalesce_max_secs:
        for key in window:
            alerted[key] = now
        state.pop("coalescing", None)
        state.pop("coalesce_started_at", None)
        persist()
        raise Report(with_warning(_coalesced_brief(list(window.values()))))

    if not persist_ok:
        # A window needs to REMEMBER when it opened, so an unwritable state
        # directory does not merely degrade coalescing -- it destroys it. Every
        # cron subprocess reloads an empty window, `elapsed` is always zero, and
        # the fire condition can never be reached: the wake is not delayed, it
        # is lost. Deliver now instead, with the warning that says why the
        # operator is about to see repeats. Without coalescing this hazard did
        # not exist (an unwritable directory only caused duplicate wakes), so it
        # arrived with the window and is guarded where the window is.
        raise Report(with_warning(_coalesced_brief(list(window.values()))))

    persist()
    raise Skip(
        f"coalescing window open {int(elapsed)}s/{int(coalesce_secs)}s, "
        f"{len(window)} anomaly(ies) coalescing, {tick.pending} pending"
    )
