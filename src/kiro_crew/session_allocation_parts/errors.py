"""The refusals the session allocation boundary raises, and the ending fence's wait bound.

``kiro_crew.session_allocation`` re-exports every name here and stays the path callers
import them from. Its own members and the ``get_or_create`` owner beside this module
both raise these, and an owner never imports the facade at run time, so they live in
the layer below both. Each class names the facade as its ``__module__``: an error chain
or a traceback renders a raised class as ``module.qualname``, and that text reaches logs
and channel replies.
"""

from __future__ import annotations


class SessionClosingError(RuntimeError):
    """A turn was requested after manager shutdown began."""


class SessionEndingError(RuntimeError):
    """A request under a key whose run is being ended could not be held until the fence lifted.

    The per-key sibling of :class:`SessionClosingError`. While a caller holds
    the key's ending fence (``begin_ending`` / ``end_ending``, the cron reaper
    and ``cancel()`` from their kill passes through the run's terminal record
    and audit), nothing lands under the key: a claim or a new allocation is HELD
    at the door of ``get_or_create`` until the fence lifts and then proceeds
    under the recorded key, and a cold start whose reservation was already in
    flight when the fence went up is refused at registration -- its started
    provider hard-killed by the same path a closing manager uses -- after which
    the same call waits for the lift and allocates again. A held request is not
    dropped: a sub-agent completion that races the reap of its parent's run is
    delivered into the session that follows the record, as it was before the
    fence existed, only never into the run being ended.

    Raised for the two requests that cannot be held: a call whose wait outlived
    :data:`ENDING_FENCE_WAIT_SECS` (a fence held far past the bounded kill
    passes it exists for -- a defect to surface, not to hang every caller of the
    key on), and a per-step task session (``open_task_session``) under a fenced
    key, which reserves only around its create and has no hard-kill path for a
    session already created on the shared runtime.
    """


class SessionBusyError(RuntimeError):
    """A caller requested an immediate turn claim while the session was held."""


#: How long a claim or cold start waits at the door of ``get_or_create`` for a
#: key's ending fence to lift before it is refused (:class:`SessionEndingError`).
#: An independent caller-refusal bound, not a multiple of the fence's length: a
#: fence covers the ending caller's kill passes (bounded resets and verified
#: kills), its terminal record (a locked store merge on a worker thread, a
#: history append) and its audit -- and a reset cancelled at its own timeout
#: still finishes its cleanup before the pass ends, so the fence's length is not
#: something this constant can be derived from. What it fixes is when a caller
#: stops waiting: well inside the 1200 s the sub-agent completion path allows
#: its whole delivery, and long enough that an ordinary ending never reaches it.
#: A fence still up at this point is a holder stuck past its own bounds: the
#: caller is refused so that the defect surfaces, rather than hanging every
#: caller of the key on it.
ENDING_FENCE_WAIT_SECS = 180.0


class SpeculativeResumeRefused(RuntimeError):
    """A speculative allocation may not consume an unrequested native resume."""


# Named by the path callers import them from, so a traceback or an error chain renders
# them exactly as before they moved here.
SessionClosingError.__module__ = "kiro_crew.session_allocation"
SessionEndingError.__module__ = "kiro_crew.session_allocation"
SessionBusyError.__module__ = "kiro_crew.session_allocation"
SpeculativeResumeRefused.__module__ = "kiro_crew.session_allocation"
