"""What a runner hands back: refusing an undriven result, and stopping returned work.

The SDK calls a runner on a worker thread and discards its return value, so a value that
is not a completed unit of work -- a suspendable, an unsettled future -- is a contract
violation the record must state. ``_undriven_result`` names it, ``_stop_returned_work``
stops returned pool work at the one instant the SDK holds it, ``_close_quietly`` retires
what remains, and ``_lazy_call_shape`` refuses at registration a callable whose call
cannot run its body. It holds to the Job SDK's doctrine (:mod:`kiro_crew.apps.job_sdk`):
a distinction stays out until the consumer that reads it exists.

Served through :mod:`kiro_crew.apps.job_sdk`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import logging
from typing import Any

#: The Job SDK logs under its facade's name, so one logger covers the whole family.
logger = logging.getLogger("kiro_crew.apps.job_sdk")


#: The ONE verdict every returned suspendable gets, worded as the contract violation
#: it is. It deliberately does NOT say the body never ran: for a drained generator it
#: did, and this SDK cannot tell that case from a suspendable closed before it began.
#: Saying "never ran" would replace a false success with a false explanation, which is
#: the same defect one layer down.
_SUSPENDABLE_RETURNED = (
    "{name}, which a runner must not return: this SDK never drives what a runner "
    "hands back, and the object itself cannot say what it did -- one that ran to "
    "completion and one closed before it began are identical from here"
)

#: The suspendable carriers, matched by kind ALONE. There is deliberately no state
#: table: the state cannot answer the question the record needs answered.
#: ``CLOSED``/``frame is None`` covers BOTH a suspendable drained to completion and one
#: closed before it ever began, and those two are indistinguishable from the object --
#: verified, not assumed (see :func:`_undriven_result` for what was measured and
#: rejected). So the classifier stops inferring history and states a rule instead.
#:
#: Two rejected alternatives, both of which look more precise and are traps:
#:
#: * the ``cr_frame``/``gi_frame``/``ag_frame`` attribute with an ``f_lasti``
#:   comparison. ``f_lasti`` is ``-1`` for an unstarted frame up to 3.10 and an
#:   instruction offset (``0``) from 3.11, so a ``< 0`` test silently matches NOTHING
#:   on 3.12 -- every ``async def`` runner would be recorded ``done`` again, the very
#:   defect this module exists to remove.
#: * ``gc.get_referents``, which DOES separate the two closed histories on 3.12:
#:   ``('str', 'str')`` for a drained generator against ``('code', 'function', 'str',
#:   'str')`` for one closed unstarted, stable across trials, creation order and an
#:   explicit ``gc.collect()``. It is still refused, because it is an undocumented
#:   interpreter internal, generators were restructured in 3.11 with lazily-created
#:   frames, and CI runs 3.10 AND 3.12. A classifier that is right on one interpreter
#:   and wrong on another writes false records confidently, which is worse than
#:   declining to classify.
_SUSPENDABLE_KINDS: tuple[tuple[str, Any], ...] = (
    ("a coroutine", inspect.iscoroutine),
    ("a generator", inspect.isgenerator),
    ("an async generator", inspect.isasyncgen),
)


#: The wording for an unsettled future this SDK could NOT stop. Every clause of it
#: is true for this branch, and consumers match on the text, so it stays verbatim.
_UNSETTLED_NOT_STOPPED = (
    "a future that is not settled, so the runner returned before its work "
    "finished; that work may still be running somewhere this SDK does not "
    "own and cannot stop, so a retry of this run can overlap it"
)

#: The wording for one this SDK DID stop, with the guarantee named. Only reachable
#: when :func:`_stop_returned_work` answered ``True``, which is the one answer that
#: means the work will never run.
_UNSETTLED_STOPPED = (
    "a future that is not settled, so the runner returned before its work "
    "finished; that work had not started and this SDK stopped it before it "
    "could, so a retry of this run cannot overlap it"
)


def _stop_returned_work(result: Any) -> bool:
    """Ask work a runner handed back to not run. ``True`` ONLY when it is stopped.

    This is the reachability half of the ownership problem. The SDK cannot stop work
    a runner spawned onto a thread it does not own -- unless the runner hands the
    work back, which a runner submitting to a pool does: it returns the unwaited
    ``concurrent.futures.Future``. At that instant this process holds the only
    reference to that work, so this is the one moment a stop is even possible.

    The stop itself was ALREADY happening before this function existed, as
    hygiene: :func:`_close_quietly` reaches for ``cancel`` on a future to stop it
    warning about an unretrieved exception, and discarded the answer. So the
    record said the work "may still be running ... and cannot stop" about work the
    next line had just guaranteed would never run. This function makes that stop
    deliberate, moves it BEFORE the verdict is worded, and reads what it returned.

    **One answer, and it is one-sided on purpose.** ``True`` means the work is
    stopped and will never run. Everything else is ``False``, which the caller
    words as the unchanged disclosure -- the work may still be running and this SDK
    cannot stop it. An earlier revision returned a third value to separate "already
    executing" from "no guarantee available"; the two collapse to the same wording
    and nothing read the difference, so by this module's own doctrine -- a
    distinction stays out until the consumer that reads it exists -- there are two
    answers, not three.

    **Why the answer is keyed on the TYPE and not on having a ``cancel`` method.**
    A truthy ``cancel()`` does not mean stopped in general. Measured identically on
    3.10, 3.11 and 3.12: an ``asyncio.Task`` whose coroutine suppresses
    ``CancelledError`` answers ``cancel()`` with ``True`` and then runs to
    completion and returns a value. Treating that as a stop would report success
    for work that is still going -- the exact defect this module exists to remove,
    and worse than reporting failure, because the owner then believes the work is
    over and proceeds.

    ``concurrent.futures.Future`` is different, and its difference is documented
    rather than inferred: ``cancel()`` returns ``False`` if the call is executing
    or finished, and otherwise the call "will be cancelled" -- the executor's
    ``_WorkItem.run`` asks ``set_running_or_notify_cancel()`` first and returns
    without invoking the function when it is cancelled. So ``True`` from THAT type
    means the work never ran. ``isinstance`` is the right test because the
    guarantee belongs to the class's contract, and ``asyncio.Future`` is not a
    subclass of it, so the two never blur.

    **The stop runs app code on this thread, so it is fenced from BaseException.**
    ``Future.cancel`` invokes the future's done callbacks inline, and
    ``_invoke_callbacks`` catches only ``Exception`` -- measured on 3.10, 3.11 and
    3.12, a callback raising ``SystemExit`` escapes ``cancel()`` while an ordinary
    ``ValueError`` is swallowed and ``cancel()`` still answers ``True``. An escape
    here is not a caller's problem to handle: it would pass ``_execute``'s
    ``except JobCancelled`` and ``except Exception`` untouched, leave ``run.status``
    at ``RUNNING``, and let the ``finally`` persist a ``running`` record while
    dropping the live entry and the dedupe key -- a run reported active that nothing
    owns, which no pass revisits until a restart makes its origin foreign. So the
    stop is attempted inside a fence: a run must never be corrupted by app code
    misbehaving inside this SDK's own hygiene.

    The fence answers ``False``, which UNDER-claims -- the state transition happens
    before the callbacks fire, so the work is in fact cancelled. Claiming it anyway
    would rest on a control flow that was just violently interrupted, and the
    hedged wording is true either way.
    """
    if not isinstance(result, concurrent.futures.Future):
        return False
    try:
        if result.done():
            return False
        # ``is True`` rather than truthiness: only the documented answer counts, so
        # a subclass returning some other truthy value cannot buy a guarantee.
        return result.cancel() is True
    except BaseException:  # noqa: BLE001 - see the fence paragraph above
        logger.warning(
            "a returned future raised while being asked to stop; recording that its "
            "work was not stopped rather than losing the run's record",
            exc_info=True,
        )
        return False


def _undriven_result(result: Any) -> str:
    """Name why a runner's return value is not a completed unit of work, else ``""``.

    The counterpart to :func:`_lazy_call_shape`, and the load-bearing half. That one
    inspects the callable's SHAPE at registration, which is a proxy: it can only
    refuse the spellings somebody thought of. This one reads the FACT, at the one
    moment it exists -- whatever the runner handed back, once.

    ONE PROTOCOL QUESTION WITH TWO CARRIERS, then allow. The question is "is this
    thing finished?", and two kinds of object answer it:

    * a SUSPENDABLE cannot answer it, so it is refused outright;
    * a FUTURE answers it by ``done()``, and then by how it settled.

    **Suspendables, ONE verdict, and this reverses an earlier decision in this same
    change set.** Earlier rounds read the frame state and allowed ``CLOSED`` on the
    reasoning that a closed frame ran to completion. That was wrong, and the reason is
    worth stating because it is the module's own thesis turned on itself: ``CLOSED``
    covers a suspendable drained to completion AND one closed before it ever began, so
    allowing it recorded a completion nobody observed -- exactly the class of claim
    this module exists to remove. The two cases were then MEASURED rather than assumed
    to differ: no public attribute, ``dir()`` entry, code object, ``gi_frame``,
    ``gi_running`` or ``gi_yieldfrom`` separates them.

    ``gc.get_referents`` does separate them on 3.12, reliably, and is refused anyway --
    see :data:`_SUSPENDABLE_KINDS` for the measurement and why depending on it would be
    a defect rather than a fix.

    So the rule replaced the inference: a returned suspendable is a CONTRACT VIOLATION
    whatever its state. The reason string says only what this SDK knows -- that it will
    not accept the object and cannot tell what it did. It deliberately does NOT say the
    body never ran, because for a drained generator it did; that wording would swap a
    false success for a false explanation. Nothing legitimate is refused: the SDK
    discards return values by design, so returning a suspendable cannot serve a runner
    even in principle.

    An async generator needs no separate case: no state is read, and reading it would
    not be portable anyway (``inspect.getasyncgenstate`` is 3.12+ while this module
    supports 3.10), so it falls under the general rule.

    **Futures, four verdicts, and deliberately NOT flattened.** Unlike a suspendable, a
    future does answer the question, so the distinctions it draws are kept. ``done()``
    False is pending, so unfinished. ``done()`` True splits, because ``done()`` answers
    "is it settled", not "did it succeed" -- and ``cancelled()`` MUST be asked before
    ``exception()``, since on a cancelled future ``exception()`` RAISES
    ``CancelledError`` rather than returning it. That is a ``BaseException``, so it
    would sail through ``_execute``'s ``except Exception`` and crash the function whose
    job is classifying failures.

    **An unsettled future is FAILED, and that verdict is true rather than
    convenient.** The runner's contract is to do its work before returning, so
    handing back something unfinished violates it -- ``failed`` is what this SDK
    observed about the RUN, not a guess about the work.

    **Whether it can STOP that work is now asked rather than assumed.** An
    unsettled ``concurrent.futures.Future`` stands for work in a pool this SDK does
    not own -- but the runner handed the future back, so the reference is in hand
    at this one instant, and :func:`_stop_returned_work` uses it. A ``True`` there
    is a documented guarantee that the work never began and never will, so the
    reason says the stop happened and a retry cannot overlap. Anything else keeps
    the earlier disclosure verbatim: the work may still be running, this SDK cannot
    stop it, and a retry can overlap it. Terminalizing still releases the dedupe
    key either way; what changed is that the owner reading the record -- the only
    party who can judge whether a retry is safe -- is told which of the two
    situations they are in instead of always being told the worse one.

    What remains missing is a stop for work whose handle never comes back at all: a
    runner that spawns a thread and returns ``None`` leaves nothing to act on, and
    no stopping logic can reach it. That half needs the spawn to register itself,
    which changes the runner contract, and is filed rather than pretended away.

    **Why this is complete, which is a fair thing to ask of a classifier.**
    Not because cases stopped being found, but because the two carriers are now
    answered on different grounds. Suspendables need no enumeration at all: every
    state, known or added by a future Python, reaches the same verdict, so there is no
    fourth state to discover and no state table to go stale. The future protocol offers
    pending, cancelled, exception and result, and this maps every one of those.

    **Default: allow.** Safe now in a way the old default was not. The old code asked
    ``isawaitable`` first, so a non-awaitable future reached the default and was
    called fine; the future carrier now catches it by ``done()``, so what still
    reaches the default is genuinely a plain return value.
    """
    # Carrier 1: a suspendable, refused by KIND -- its state is not consulted,
    # because no state it can report answers "did the work happen".
    for name, is_kind in _SUSPENDABLE_KINDS:
        if is_kind(result):
            return _SUSPENDABLE_RETURNED.format(name=name)

    # Carrier 2: a future, which answers by `done()` and then by how it settled.
    done = getattr(result, "done", None)
    if callable(done):
        if not done():
            # The ONE side effect in this function, and it is here because the
            # verdict depends on it: this is the only instant the SDK holds a
            # reference to the spawned work, so the stop has to be attempted
            # before the record can say what became of that work.
            return _UNSETTLED_STOPPED if _stop_returned_work(result) else _UNSETTLED_NOT_STOPPED
        cancelled = getattr(result, "cancelled", None)
        if callable(cancelled) and cancelled():
            return "a cancelled future, so the work it stood for never completed"
        exception = getattr(result, "exception", None)
        if callable(exception):
            # Safe without a timeout because ``done()`` is established, so it cannot
            # block. Retrieving it also MARKS it retrieved, which suppresses the
            # "never retrieved" warning :func:`_close_quietly` handles for the
            # unsettled case -- classification and quiet retirement meet here.
            exc = exception()
            if exc is not None:
                return (
                    f"a future that failed with {type(exc).__name__}, "
                    "so the work it stood for ran and raised"
                )
        return ""

    # Carries neither answer and still needs driving: nothing here will do it.
    if inspect.isawaitable(result):
        return _SUSPENDABLE_RETURNED.format(name="a bare awaitable")

    return ""


def _close_quietly(result: Any) -> None:
    """Close an undriven result so it does not warn from the garbage collector.

    A coroutine that is never awaited emits ``RuntimeWarning: coroutine ... was
    never awaited`` when collected, at a point far from the run that caused it.
    The record already says what happened, so the warning is noise pointing at
    the wrong place.

    Two shapes, because they do not share a method. Coroutines and generators
    expose a synchronous ``close``. A future does NOT -- neither ``asyncio.Future``
    nor ``concurrent.futures.Future`` has ``close``, so an earlier version of this
    docstring named futures among the closable and was wrong about the one object
    the sentence was about. A pending future is retired with ``cancel`` instead,
    which is what stops it warning that its exception was never retrieved. An
    async generator's ``aclose`` is itself a coroutine needing a loop, so it is
    left to the interpreter rather than driven from this thread.

    This ``cancel`` is HYGIENE only. Stopping the work a future stands for is
    :func:`_stop_returned_work`'s job, which runs earlier and reads what the stop
    returned. The two are not duplicates: that one answers
    a question the record needs, this one silences a warning, and by the time this
    runs on a future the earlier call has usually already settled it -- a second
    ``cancel`` on a cancelled or running future returns ``False`` and invokes no
    callbacks, since those fire once, at the state transition. Anything that call
    declined to touch still arrives here and is retired exactly as before.

    ``except Exception`` here is narrower than the fence
    :func:`_stop_returned_work` uses, and deliberately so rather than by oversight.
    Both calls can run app code -- a done callback, a coroutine's ``finally`` -- and
    a ``BaseException`` from either escapes ``_execute``'s handlers. The
    consequences differ: this call site is reached only AFTER ``run.status`` and
    ``run.error`` are set, so the ``finally`` still persists a correct terminal
    record and the cost of an escape is one lost log line. The earlier call runs
    while the status is still ``RUNNING``, where the same escape persists a
    ``running`` record nothing owns. Widening this one would change pre-existing
    behaviour to no end.
    """
    for method in ("close", "cancel"):
        handler = getattr(result, method, None)
        if not callable(handler):
            continue
        try:
            handler()
        except Exception:  # noqa: BLE001 - closing is hygiene, never the outcome
            logger.debug("could not retire an undriven job result", exc_info=True)
        return


def _lazy_call_shape(fn: Any) -> str:
    """Name the shape of a callable whose CALL does not run its body, else ``""``.

    Three shapes return a lazy object instead of doing the work: a coroutine
    function, an async generator function, and a plain generator function. The
    SDK calls its runner on a worker thread and discards the return value, so for
    all three the body never executes. Before the execution-side check existed
    such a run was then recorded ``done``; today :func:`_undriven_result` reads the
    returned object and records ``failed``, so this guard is a fast, friendly error
    at registration rather than the safety property.
    One defect with three spellings, so the check names the class rather than the
    one spelling that was reported.

    A callable OBJECT is examined through its ``__call__``, because that is what
    invocation actually reaches -- ``inspect`` reports the instance itself as an
    ordinary object, so a check written against bare functions passes it through.
    That was the gap two reviewers found in the first version of this guard.

    No legitimate runner is refused by this: a callable whose invocation returns a
    lazy object cannot do the work the SDK is calling it to do, whatever it is.
    """
    for target in (fn, getattr(fn, "__call__", None)):
        if target is None:
            continue
        if inspect.iscoroutinefunction(target):
            return "a coroutine function"
        if inspect.isasyncgenfunction(target):
            return "an async generator function"
        if inspect.isgeneratorfunction(target):
            return "a generator function"
    return ""
