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

#: Hard wall-clock bound on a coalescing window, measured from the OLDEST entry
#: still in it. Reached only when ``pending`` never drains (a check wedged in
#: queued, or a phantom pending row). Independent of ``pending`` on purpose: it
#: is what guarantees a delayed wake rather than a lost one, and reading the
#: oldest entry keeps that guarantee per entry -- ``oldest`` is never below any
#: single entry's age, so nothing outlives this bound counted from its own
#: arrival.
DEFAULT_COALESCE_MAX_SECS = 1800.0

#: Cap on how many observation labels a coalesced wake spells out.
_MAX_LIST = 8

#: Every dedupe key the kernel stores carries exactly one of these sentinels,
#: so an epoch reset can keep the epoch-independent half without inspecting the
#: probe's key text. A probe never writes the sentinel itself and its keys are
#: opaque to the kernel, so prefixing unconditionally is what makes the two
#: spaces impossible to confuse -- a scheme that only prefixed the sticky half
#: could be spoofed by a probe whose own key happened to start with it.
_EPOCH_SENTINEL = "="
_STICKY_SENTINEL = "~"


def _dedupe_key(obs: "Observation") -> str:
    """The key an observation is remembered under, sentinel included."""
    return (_EPOCH_SENTINEL if obs.epoch_scoped else _STICKY_SENTINEL) + obs.key


def _migrate_key(key: str) -> str:
    """Adopt a dedupe key written before the sentinels existed.

    State persisted by an earlier version carries bare probe keys. Read as-is
    they would never match a key this version computes, so every armed watch
    would wake once more for anomalies it had already reported -- a small but
    entirely avoidable upgrade blip. A bare key is adopted as epoch scoped,
    which is what every pre-sentinel key was: the sticky space did not exist.

    ``blind`` is the kernel's own error-backstop marker rather than a probe key,
    and it is looked up by that literal name, so it must stay unprefixed.
    """
    if key == "blind" or key.startswith((_EPOCH_SENTINEL, _STICKY_SENTINEL)):
        return key
    return _EPOCH_SENTINEL + key


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
        epoch_scoped: Whether this observation describes the CURRENT epoch.

            True (the default) is the check-rollup shape: the anomaly is a
            property of the thing the epoch names, so when the epoch changes
            the observation is about something that no longer exists and its
            dedupe memory is correctly wiped.

            False is for a signal observed through the same probe that is
            NOT a property of the epoch -- a comment on a pull request belongs
            to the conversation, not to the commit under review. Left epoch
            scoped, every such signal would be re-reported in full the tick
            after any epoch change: a force-push would replay every comment
            ever seen as though it had just arrived. The kernel keeps these
            keys across an epoch reset instead.
    """

    key: str
    severity: Severity
    brief: str = ""
    epoch_scoped: bool = True


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

    def wake_suffix(self) -> str:
        """Optional text appended ONCE per wake, after every brief.

        A brief describes ONE observation; this describes the WAKE. Standing
        instructions to the woken agent ("the watch stays armed", "read the
        ledger first") and the operator's own context note belong here, because
        they are true of the delivery rather than of any single signal.

        Putting them in the brief instead is what the split exists to prevent:
        the kernel joins N briefs into one body, so per-observation text is
        repeated N times, and the better coalescing works the more it repeats.
        Measured on a real six-observation wake, that duplication was 56% of the
        delivered bytes -- context the woken agent pays for and cannot use.

        Called after :meth:`identity`, so a probe may derive it from its own
        cron message. A value that is not a string, or one that raises, is
        dropped rather than allowed to kill the tick: a wake with no footer is
        recoverable, a watch that raises every tick is auto-paused.
        """
        return ""


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
                kept[_migrate_key(key)] = ts
        state["alerted"] = kept
    errors = data.get("errors")
    if isinstance(errors, int) and not isinstance(errors, bool) and errors >= 0:
        state["errors"] = errors
    # Read, never stored: the local value below seeds legacy window rows with the
    # age they were persisted with, and nothing reads the key back out of state.
    # Not storing it is what lets the legacy field fall off at the first
    # post-upgrade write instead of riding along until the window closes.
    started = _coerce_ts(data.get("coalesce_started_at"))
    window_rows = data.get("coalescing")
    if isinstance(window_rows, dict):
        pending_wakes: dict[str, dict] = {}
        for key, row in window_rows.items():
            if not isinstance(key, str):
                continue
            if isinstance(row, str):
                # Written before an entry carried its own open time: the whole
                # window shared one stamp, so seed every entry from it. Reading
                # the old shape as unstamped instead would restart the clock of
                # a window already open on disk, and an in-flight watch would
                # serve a second floor across the version change.
                brief, opened = row, started
            elif isinstance(row, dict) and isinstance(row.get("brief"), str):
                brief, opened = row["brief"], _coerce_ts(row.get("opened_at"))
            else:
                continue
            entry: dict = {"brief": brief}
            if opened is not None:
                entry["opened_at"] = opened
            pending_wakes[_migrate_key(key)] = entry
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


def _install_window(state: dict, window: dict[str, dict]) -> None:
    """Store the open coalescing window, or close it when nothing is left.

    Every entry carries its OWN ``opened_at``, because one window legitimately
    holds signals of different ages: a partial fire leaves entries that have
    already waited, and the next tick can add one that has not waited at all.
    That per-entry stamp is the only age this module stores.

    There is deliberately no window-level stamp to write or clear:
    ``coalesce_started_at`` is read once in :func:`load_state`, to seed entries
    persisted before they carried their own, and never stored again.
    """
    if window:
        state["coalescing"] = window
        return
    state.pop("coalescing", None)


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
    current epoch and every entry carries its own open time. An entry TRIGGERS a
    wake when::

        age >= coalesce_secs and (pending == 0 or the entry is epoch-independent)

    and the wake then carries every entry the same population gate admits, aged
    or not. ``pending`` gates epoch-scoped entries only, because it counts CHECKS
    and a comment is complete the moment it is posted. Separately, when the OLDEST
    entry's age passes ``coalesce_max_secs`` the whole window flushes: that cap is
    an absolute wall and is not gated behind the floor.

    The trigger is per ENTRY because one window holds signals of different ages --
    a partial fire leaves entries that have already waited, and the next tick can
    add one that has not waited at all, which must not wake the agent on its own.
    The payload stays generous for the opposite reason: riding along never adds a
    wake, while holding an admitted entry back guarantees another one later.

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

    # The per-wake footer, read once here for the same reason tuning() is: after
    # identity(), so a probe may derive it from its cron message. Guarded the
    # same way too -- a footer is not worth a crash loop, and a probe that
    # returns a non-string would otherwise blow up inside the join.
    try:
        raw_suffix = probe.wake_suffix()
    except Exception:
        logger.warning("irq: probe wake_suffix() raised; wake has no footer", exc_info=True)
        raw_suffix = ""
    wake_suffix = raw_suffix if isinstance(raw_suffix, str) else ""

    def body(briefs: list[str]) -> str:
        """The delivered wake: every brief, then the footer ONCE."""
        joined = _coalesced_brief(briefs)
        if not wake_suffix:
            return joined
        return f"{joined}\n\n{wake_suffix}" if joined else wake_suffix

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
            blind = Skip(f"probe failed ({errors} consecutive; blind alert deduped)")
            blind.blind = True  # type: ignore[attr-defined]
            raise blind
        # BLIND, not unchanged. A cron driver treats every Skip the same -- sleep
        # until the next tick -- so this distinction never had to exist. A driver
        # that decides whether to SPEND on the strength of a Skip does need it: a
        # failed fetch means the subject was not observed at all, and reading that
        # as "nothing changed" is how an expired credential or a network outage
        # turns into silence. The flag rides the exception so no caller has to
        # match on the message text.
        failed = Skip(f"probe failed ({errors} consecutive)")
        failed.blind = True  # type: ignore[attr-defined]
        raise failed

    if state.get("errors"):
        state["errors"] = 0
        # A recovered streak clears the blind marker so the NEXT streak
        # alerts promptly instead of inheriting up to realert_secs of dedupe.
        state.get("alerted", {}).pop("blind", None)

    epoch_changed = bool(tick.epoch and state.get("epoch") != tick.epoch)
    if epoch_changed:
        # The subject became a different subject: fresh epoch, fresh memory.
        #
        # Sticky state is the exception, and the reason the sentinel exists: a
        # signal that is not a property of the epoch (a comment on the pull
        # request rather than a check on the commit) has not stopped being true
        # just because the head moved. Wiping those would replay the entire
        # conversation on the tick after every force-push. ``blind`` is
        # deliberately NOT carried over: it records that the probe could not
        # observe at all, and a fresh epoch deserves a fresh judgement on that.
        #
        # BOTH halves of sticky state have to survive, which is easy to get half
        # right. `alerted` is what stops a delivered signal repeating. The open
        # `coalescing` window is a signal that has NOT been delivered yet, and
        # dropping it destroys the wake outright: the probe may no longer report
        # that observation (a comment ages past its horizon), so nothing puts it
        # back. Epoch-scoped window entries ARE dropped -- they describe checks
        # on a commit that is no longer under review.
        #
        # Keep each carried entry's own stamp so a force-push does not make an
        # already-settled comment pay the floor again. A freshly pushed commit
        # briefly shows an almost-empty rollup, so ``pending == 0`` can be true
        # while nothing has run. Per-entry ages keep that case separate: a new
        # epoch-scoped ``ready`` observation gets a new stamp and therefore its
        # own full floor, regardless of the carried comment's age.
        carried = {
            key: value
            for key, value in (state.get("alerted") or {}).items()
            if isinstance(key, str) and key.startswith(_STICKY_SENTINEL)
        }
        carried_window = {
            key: {"brief": row["brief"], "opened_at": row.get("opened_at")}
            for key, row in (state.get("coalescing") or {}).items()
            if isinstance(key, str)
            and key.startswith(_STICKY_SENTINEL)
            and isinstance(row, dict)
            and isinstance(row.get("brief"), str)
        }
        state = {"epoch": tick.epoch, "alerted": carried, "errors": 0}
        if carried_window:
            state["coalescing"] = carried_window

    alerted = state.setdefault("alerted", {})
    now = time.time()

    # Epoch-scoped keys are bounded by the epoch reset that wipes them. Sticky
    # keys have no such bound, so a long-lived watch on a busy subject would
    # accumulate them forever. Drop the ones already past the re-alert window:
    # they no longer suppress anything (``should_alert`` would return True for
    # them anyway), so this frees state without changing any decision. A probe
    # that must never re-report such a signal has to filter it out on its own
    # side -- which is why the pull-request probe ignores comments older than
    # its horizon, and why that horizon has to stay under ``realert_secs``.
    for stale in [
        key
        for key, value in alerted.items()
        if isinstance(key, str)
        and key.startswith(_STICKY_SENTINEL)
        and (ts := _coerce_ts(value)) is not None
        and now - ts >= realert_secs
    ]:
        alerted.pop(stale, None)

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
        done = Done(with_warning(body([o.brief for o in terminal if o.brief])))
        # The KEYS travel with the exception, because "the watch ended" and "it
        # ended well" are different facts and only the probe knows which. A
        # driver that records a durable outcome has to tell a merged subject from
        # one closed without merging, and the alternative -- matching on the
        # delivered human text -- would break the first time that wording is
        # edited. Additive: the cron driver ignores it.
        done.keys = tuple(o.key for o in terminal)  # type: ignore[attr-defined]
        raise done

    for obs in tick.observations:
        if obs.severity is Severity.NMI and should_alert(_dedupe_key(obs)):
            alerted[_dedupe_key(obs)] = now
            persist()
            raise Report(with_warning(body([obs.brief])))

    fresh_wakes = {
        _dedupe_key(o): o.brief
        for o in tick.observations
        if o.severity is Severity.WAKE and should_alert(_dedupe_key(o))
    }

    if coalesce_secs <= 0:
        if fresh_wakes:
            for key in fresh_wakes:
                alerted[key] = now
            persist()
            raise Report(with_warning(body(list(fresh_wakes.values()))))
        persist()
        raise Skip(tick.detail or f"{tick.pending} pending")

    # Prune before extending: an anomaly that CLEARED while the window was
    # open must not be reported. Without this an entry lives until the window
    # fires, so a check that reran green would still be announced as failing,
    # potentially in the same brief as the all-green observation that replaced
    # it -- a wake that contradicts itself, and the operator has no way to tell
    # which half is current.
    # The window, ``observed_wakes`` and ``alerted`` all key on the SENTINEL-
    # prefixed form, so the prune below compares like with like. Mixing raw and
    # prefixed keys here would make every entry look cleared and silently
    # disable coalescing.
    #
    # STICKY entries are exempt from the prune, and the asymmetry is the point.
    # For an epoch-scoped observation, "the probe stopped reporting it" means the
    # condition cleared, which is what makes pruning correct. For an
    # epoch-independent one it means the probe stopped LOOKING -- a comment ages
    # out of the pull-request probe's horizon while remaining just as true -- so
    # pruning on that DESTROYS a wake rather than delaying it. A signal first
    # observed shortly before its horizon expires is exactly the case that hits
    # this, and it is reachable on the shipped defaults.
    #
    # The cost accepted is staleness instead of loss: a decision-style sticky key
    # superseded inside one window (CHANGES_REQUESTED, then APPROVED) now fires
    # alongside its successor rather than vanishing. Both land in one brief and
    # the woken agent reads live state regardless, which is strictly better than
    # never being told a human had blocked the PR.
    observed_wakes = {_dedupe_key(o) for o in tick.observations if o.severity is Severity.WAKE}
    window: dict[str, dict] = {
        key: row
        for key, row in (state.get("coalescing") or {}).items()
        if key in observed_wakes or key.startswith(_STICKY_SENTINEL)
    }
    # A fresh signal joins with its OWN open time. Re-observing an entry already
    # in the window refreshes its BRIEF and never its stamp: a probe reports an
    # unresolved anomaly on every tick until it clears, so restamping here would
    # reset its clock each time and the window would never reach its floor.
    for key, brief in fresh_wakes.items():
        row = window.get(key)
        if row is None:
            window[key] = {"brief": brief, "opened_at": now}
        else:
            row["brief"] = brief
    if window:
        # A row with no usable stamp opens its clock now. Two producers reach
        # here: a sticky entry carried across an epoch reset, which restarts by
        # design (see above), and persisted state whose window stamp was itself
        # unusable. A stamp in the FUTURE is a clock rollback, and treating it as
        # opening now is what keeps it from firing at once or never. Both cost a
        # delay, never a loss.
        for row in window.values():
            opened = _coerce_ts(row.get("opened_at"))
            if opened is None or opened > now:
                row["opened_at"] = now
        _install_window(state, window)
        # Persist BEFORE evaluating the fire condition, so ``persist_ok`` below
        # reflects this tick's write rather than a stale initial value: whether
        # the window can be remembered is exactly what decides if delaying it is
        # safe.
        persist()
    else:
        # Everything in flight cleared. Close the window rather than leaving a
        # start stamp behind, or the NEXT anomaly would inherit this window's
        # age and could fire without any settling time of its own.
        _install_window(state, window)
        persist()
        raise Skip(tick.detail or f"{tick.pending} pending")

    converged = tick.pending == 0
    ages = {key: now - float(row["opened_at"]) for key, row in window.items()}
    oldest = max(ages.values(), default=0.0)
    # The cap is an ABSOLUTE wall, and it stays a WINDOW-level one that flushes
    # everything. It must not be gated behind the floor -- written as
    # `age >= floor and (converged or age >= cap)` it was, a caller passing a
    # floor above the cap (legal, both finite and positive) plus a pending count
    # that never drains meant the cap could never be reached first, and the
    # guarantee it exists to make -- a delayed wake rather than a dropped one --
    # was quietly void for those values.
    #
    # Making the cap per entry too was tried and reverted: withholding an
    # unconverged neighbour from a cap wake turns ONE flush into one wake per
    # entry for a subject whose ``pending`` never drains, which is the per-wake
    # cost this window exists to remove and the opposite of what the payload rule
    # below is for. So the cap's PAYLOAD is what it always was: everything.
    #
    # Its CLOCK does move, and the one scenario is worth naming. Base measured
    # the cap from a window stamp deliberately retained across a partial fire;
    # this reads the oldest SURVIVING entry, so a partial fire that delivers the
    # entry which opened the window defers the cap wake by that entry's head
    # start. Keeping the delivered entry's stamp alive just to preserve the old
    # instant would rebuild the very conflation this change removes -- a later
    # joiner would then flush on a clock it never spent. What the cap actually
    # promises is a bound, and the bound survives per entry: ``oldest`` is never
    # below any single entry's age, so no entry outlives the cap measured from
    # its own arrival. The shift is therefore always toward a LATER wake, never
    # an earlier one and never a lost one.
    if oldest >= coalesce_max_secs:
        fire = {
            key: row["brief"]
            for key, row in window.items()
            if not (
                epoch_changed and not key.startswith(_STICKY_SENTINEL) and ages[key] < coalesce_secs
            )
        }
        triggered = bool(fire)
    else:
        # Two separate questions, and keeping them separate is the whole change.
        #
        # (1) May an entry TRIGGER a wake? Only once it has served a floor of its
        #     OWN. One window-level age could not answer that, because one window
        #     holds signals of different ages: a partial fire leaves entries that
        #     have already waited, and the next tick can add one that has not
        #     waited at all. The joining entry inherited an age it never spent and
        #     woke the agent on the very next tick, so a burst arriving one at a
        #     time cost one wake each.
        #
        # (2) Given that a wake IS going out, which entries ride along? Every
        #     entry this tick's population gate admits, aged or not, which is
        #     unchanged. Riding along never ADDS a wake, and holding an admitted
        #     entry back would guarantee another one later, so the answer here has
        #     to stay generous or coalescing stops coalescing.
        #
        # The population gate: past the floor, an epoch-scoped entry additionally
        # waits for ``pending`` to drain and a STICKY one does not. ``pending``
        # counts CHECKS, which makes it the right gate for an epoch-scoped anomaly
        # -- a draining check is exactly what can still resolve one -- and the
        # wrong gate for a comment, which is complete the moment it is posted and
        # does not become truer when a check finishes. Measured on a real pull
        # request with 18 checks in flight, a fresh review comment was held the
        # full 30 minutes for no observation it could have gained. Admitting the
        # epoch-scoped half on a sticky signal's readiness instead would announce
        # a `ready` before the new head's checks existed -- the
        # convergence-that-never-happened the floor was added to prevent.
        fire = {}
        triggered = False
        for key, row in window.items():
            if not (converged or key.startswith(_STICKY_SENTINEL)):
                continue
            # A carried sticky entry may already be old enough to trigger on
            # the transition tick. Do not let a brand-new head observation ride
            # on that wake before serving its own floor.
            if epoch_changed and not key.startswith(_STICKY_SENTINEL) and ages[key] < coalesce_secs:
                continue
            fire[key] = row["brief"]
            if ages[key] >= coalesce_secs:
                triggered = True
        if not triggered:
            fire = {}
    # `persist_ok` gates the partial fire, and the reason is the fallback below.
    # Withholding the epoch-scoped half is only a DELAY while the window can be
    # remembered; with an unwritable state directory the next tick reloads an
    # empty window, so withholding becomes a LOSS -- the exact hazard the
    # persistence fallback exists to close, reintroduced for the half this branch
    # holds back. When the write failed, fall through and deliver everything now.
    if fire and persist_ok:
        for key in fire:
            alerted[key] = now
        remaining = {k: v for k, v in window.items() if k not in fire}
        # The remainder keeps its OWN stamps, so a partial fire never pushes it
        # out: those entries have been waiting since they opened, and restarting
        # their clock each time a sticky signal arrives would turn a talkative
        # pull request into an indefinite delay for the check anomaly beside it.
        _install_window(state, remaining)
        persist()
        raise Report(with_warning(body(list(fire.values()))))

    if not persist_ok:
        # A window needs to REMEMBER when it opened, so an unwritable state
        # directory does not merely degrade coalescing -- it destroys it. Every
        # cron subprocess reloads an empty window, every age is always zero, and
        # the fire condition can never be reached: the wake is not delayed, it
        # is lost. Deliver now instead, with the warning that says why the
        # operator is about to see repeats. Without coalescing this hazard did
        # not exist (an unwritable directory only caused duplicate wakes), so it
        # arrived with the window and is guarded where the window is.
        raise Report(with_warning(body([row["brief"] for row in window.values()])))

    persist()
    raise Skip(
        f"coalescing window open {int(oldest)}s/{int(coalesce_secs)}s, "
        f"{len(window)} anomaly(ies) coalescing, {tick.pending} pending"
    )


# --------------------------------------------------------------- driver front door


class Outcome(Enum):
    """What one tick tells a non-cron driver to do."""

    #: Nothing to service. The driver must not spend a model turn.
    QUIET = "quiet"
    #: Service this now; :attr:`Verdict.body` is the wake text.
    WAKE = "wake"
    #: The subject is finished. Stop watching it.
    TERMINAL = "terminal"
    #: The kernel could not reach a verdict. The driver keeps whatever
    #: schedule it already had, so behaviour is unchanged rather than silent.
    FALLBACK = "fallback"


@dataclass(frozen=True)
class Verdict:
    """One tick's instruction to a driver, as a value rather than an exception."""

    outcome: Outcome
    #: Delivered text for WAKE and TERMINAL; a log-only reason otherwise.
    body: str = ""
    #: For TERMINAL only: the probe's own keys for why the watch ended, so a
    #: driver can record an outcome without parsing the delivered prose. Empty
    #: whenever the kernel could not attribute the end to a probe observation --
    #: an unusable target, say -- which a driver must treat as "ended, not
    #: necessarily well" rather than assuming success.
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DriverJob:
    id: str


@dataclass(frozen=True)
class _DriverCtx:
    """The whole context the kernel needs, for a driver that has no cron job.

    The kernel reads exactly two things off a ctx -- ``job.id`` for the state
    identity and (through the probe) ``message`` for configuration -- both via
    ``getattr`` with a default, and :func:`run` types its parameter as a bare
    ``object``. So the kernel was never cron-specific; this makes that a named,
    tested entry point instead of a property a caller has to rediscover.
    """

    job: _DriverJob
    message: str


def poll(
    identity: str,
    message: str,
    probe: Probe,
) -> Verdict:
    """Run ONE tick and return its verdict instead of raising it.

    This is the entry point for a driver that is not a script cron -- an
    in-process scheduler that owns its own wake mechanism and only needs the
    kernel's *decision*. :func:`run` stays exactly as it is: its raise-based
    contract is what the cron runner consumes, and rewriting it would churn the
    one shipped probe for no gain.

    ``identity`` replaces the cron job id in the state digest, so two drivers
    watching one subject keep independent dedupe memories. Pass something stable
    for the life of the watch (a loop id), never something regenerated per tick
    -- a fresh identity is a fresh memory, which re-wakes on signals already
    serviced.

    ``message`` is the probe's configuration, in the same shape the probe already
    parses off a cron message, so a probe needs no change to be driven here.

    The kernel's bounds are deliberately NOT forwarded from here. A probe already
    declares what it needs through :meth:`Probe.tuning`, no caller of this
    function passes a bound, and a passthrough with no producer is a contract
    nobody exercises -- the second driver would then build on a shape that was
    never tested. Add the parameter when a driver needs it.

    **Failure direction is deliberate.** Anything unexpected -- a probe bug, a
    kernel contract break -- resolves to :attr:`Outcome.FALLBACK`, which tells
    the driver to keep the schedule it already had. The alternative default,
    QUIET, would convert a bug into silence: the driver would stop waking and
    the work it was watching would stall with nothing to show why. A redundant
    cycle costs tokens; a lost wake costs the task.
    """
    ctx = _DriverCtx(job=_DriverJob(id=str(identity or "")), message=str(message or ""))
    try:
        run(ctx, probe)  # type: ignore[arg-type]
    except Skip as exc:
        if getattr(exc, "blind", False):
            # The subject was NOT observed -- the probe could not reach it. QUIET
            # would assert "nothing changed", which is a claim this tick has no
            # standing to make, and it is how an expired credential becomes an
            # indefinitely silent watch. Failure resolves toward spending.
            return Verdict(Outcome.FALLBACK, str(exc))
        return Verdict(Outcome.QUIET, str(exc))
    except Report as exc:
        return Verdict(Outcome.WAKE, str(exc))
    except Done as exc:
        keys = getattr(exc, "keys", ())
        return Verdict(
            Outcome.TERMINAL,
            str(exc),
            tuple(str(k) for k in keys) if isinstance(keys, tuple) else (),
        )
    except Exception:
        # A probe is documented as not raising for an expected failure (it
        # returns Tick(fetch_ok=False) and lets the kernel own the backstop), so
        # arriving here means a defect. Log it once per tick and degrade.
        logger.warning(
            "irq: poll(%s) raised; falling back to the driver's own schedule",
            identity,
            exc_info=True,
        )
        return Verdict(Outcome.FALLBACK, "probe raised")
    # run() raises on every path; a plain return is a contract break, not a
    # quiet tick. Degrade the same way rather than inventing a decision.
    logger.warning("irq: poll(%s) returned without a verdict", identity)
    return Verdict(Outcome.FALLBACK, "no verdict")
