"""Write the ACP turn lifecycle to an append-only per-session crew log.

See ``docs/system-specs/modules/crew-log-emitter.md`` for the contract this
module implements. In short: each entry point is a one-line call at a site in the
dashboard chat path where a lifecycle fact is already known, and every one of
them is **fail-soft** -- a crew log error is reported once and then swallowed, so a
broken crew log can never break a turn.

The module is on by default and inert when ``KIROCREW_CREW_LOG`` is set to a
falsy value (``0``/``false``/``no``/``off``). With the flag off nothing is
created and every call returns immediately.

The grouping identity of a session entry is ``data.turn`` -- the runner's own turn
ordinal -- and, inside a turn, ``data.step``, the MODEL CALL the entry belongs to.
A tool entry also carries ``data.call_index``, its position among that turn's tool
calls, which is what ``step`` meant before a model call had a name of its own: one
model call can issue several tools at once, so the step cannot order them. All are
known at EMIT time, so an entry answers "what happened in this turn" on its own,
with nothing to look up and no earlier line to resolve against.

The envelope's ``thread`` field is left unset here. ``thread`` points at another
LINE's ``seq``, which only a unit whose anchor line is written before the entries
citing it can supply; that is the crew's log shape, not this one's.

Three things are recorded differently from a naive reading of the lifecycle, all
deliberate and all explained in the spec:

* ``compaction/applied`` carries context-usage **percentages**, because the
  compaction boundary never learns a raw token count.
* A tool call and an approval are identified by an id inside ``data``, not by
  ``ref``. A ``Ref`` is a citation of another crew log's lines, and a tool call id
  names a frame on the ACP stream, which is not a crew log unit.
* A turn that ends without its terminal event is still CLOSED, by the turn's own
  ``finally``: ``stop_reason: "failed"``, an ``error`` naming the exception class
  when one was caught, and no ``tokens`` or ``credits``, because none were
  measured. Leaving the start open would say the writer died, which this process
  being alive contradicts -- and nothing here would correct it, since the
  interrupted-turn repair is opt-in and only a resume or a supersede asks for it.
  A recovery re-entry anchors its own thread carrying ``depth``.

**Storage never runs on the caller's thread when that thread is the event
loop.** Every entry point is called from the dashboard's async chat path, and
the storage call underneath takes the unit's lock, reads a bounded tail to
assign ``seq`` and ``fsync``s the appended line -- a waiting ``flock`` and a
kernel ``fsync`` once per tool frame and once per turn, on the one loop that
also drives the liveness heartbeat. So an entry point does only what must be
measured where it is called and builds the storage call as a job for the durable
writer, :class:`kiro_crew.crew_log.writer.CrewLogWriter`, whose single worker (the
pool :func:`kiro_crew.executors.crew_log_executor` builds) drains the work in the
order the call sites produced it.

**Which turn an entry belongs to is carried IN the entry, not looked up.** Every
session entry that belongs to a turn names it in ``data.turn`` -- the runner's own
turn ordinal, which the call site already holds -- plus ``data.step`` for the model
call it happened in, and a tool entry also carries ``data.call_index``. So nothing
has to be cached,
read back from a line written earlier, or kept alive across a queue: an entry is
self-describing the moment it is built, and a lost or evicted piece of in-process
state cannot make it name the WRONG turn.

The envelope's ``thread`` field stays unset on a session entry. ``thread`` points
at another LINE's seq, which is only knowable for a unit whose anchor line is
written before the entries that cite it; that is the crew's log shape, not
this one's.

**Two collaborators own the rest, and this module installs both.** The durable
write policy -- the buffer and its ceilings, retention and backoff, the
``write/dropped`` loss marker, the inline path, the shutdown drain -- is
:mod:`kiro_crew.crew_log.writer`. The live-turn memos -- the step and call ordinals,
the settle-once tool registry, the attempt counts, the supersede repair's debt -- are
:class:`kiro_crew.crew_log.turn_tracker.TurnTracker`. Both are imported and built on
first use rather than at import, for the boot-path reason :func:`_crew_log` gives,
and :func:`reset_caches` retires both so the next use builds fresh ones. New work on
how an append reaches the file belongs in the writer; new per-turn bookkeeping belongs
in the tracker; what a lifecycle fact records belongs here.

With no event loop running the write happens inline on the calling thread --
which is what makes a synchronous caller, and the test suite, deterministic.
:func:`flush` waits for the buffers to drain when a caller needs the file on disk
before it looks, and :func:`drain_for_shutdown` is the quiescence barrier a
restart needs: buffered entries are in memory, so an exit that skips it loses the
last thing each session did.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.constants import CREW_LOG_ENV
from kiro_crew.crew_log.emit_children import (  # noqa: F401
    dismiss_child,
    on_subagent_completed,
    on_subagent_dismissed,
    on_subagent_failed,
    on_subagent_spawned,
    on_subagent_steered,
)
from kiro_crew.crew_log.emit_crew import (  # noqa: F401
    _KIND_CREW,
    CREW_DISPATCH,
    CREW_REPORT,
    _crew_append,
    _crew_thread,
    _crew_unit,
    _dispatch_target_ok,
    _max_ref_span,
    _newest_dispatch_for,
    crew_src,
    on_crew_dispatch,
    on_crew_report,
)
from kiro_crew.crew_log.emit_runtime import (  # noqa: F401
    _EAGER_SETTLE_FLOOR_SECONDS,
    _MAX_CHILD_ORIGINS,
    _MAX_REPAIR_WALKED,
    _MAX_SESSION_ID_CHARS,
    _ORIGIN_REAP_SCAN,
    _REPAIR_WALK_HOPS,
    _SHUTDOWN_DRAIN_SECONDS,
    _bound_open,
    _child_gone_probe,
    _closing,
    _ensure_shutdown_hook,
    _hand_over,
    _handle,
    _late_handle,
    _late_retry_delay,
    _nothing_to_release,
    _notify_growth,
    _queue_tail_repair,
    _reap_child_origin,
    _remember,
    _repair_superseded,
    _report,
    _retry_delay,
    _settle_rejected,
    _submit,
    _terminal_dropped,
    _tracker,
    _walk_to_predecessor,
    _warnings,
    _write,
    _writer,
    _writer_owes,
    _writer_pool,
    add_growth_listener,
    buffered_writes,
    child_origin,
    dispatch_origin,
    drain_for_shutdown,
    dropped_writes,
    flush,
    forget_child_origin,
    live_turn,
    lost_child_origins,
    on_session_closed,
    open_child_origin,
    overflow_writes,
    peak_buffered_writes,
    remember_child_origin,
    reset_caches,
    set_child_liveness,
)
from kiro_crew.crew_log.emit_session import (  # noqa: F401
    _DEFAULT_AGENT,
    _latch_class,
    _note_class_change,
    _seed_attempts,
    on_class_observed,
    on_open_tab_unrestored,
    on_session_adopted,
    on_session_opened,
    on_session_released,
    on_session_spawned,
)
from kiro_crew.crew_log.emit_state import (  # noqa: F401
    _KIND,
    _MAX_OPEN_CREW_LOGS,
    _SRC_ACP,
    _SRC_GATEWAY,
    _WORK_TYPE,
    _child_origin,
    _creation_failed,
    _crew_log,
    _growth_listeners,
    _install_lock,
    _last_class,
    _last_config,
    _lock,
    _note_eager,
    _open,
    _repair_walked,
    enabled,
    logger,
    session_id_of,
)
from kiro_crew.crew_log.emit_surfaces import (  # noqa: F401
    _CARD_ID_MAX,
    _CARD_TEXT_MAX,
    _append_dashboard,
    _card_id,
    dashboard_entry_fits,
    ledger_entry_fits,
    on_card_finished,
    on_card_proposed,
    on_dashboard_agentic,
    on_dashboard_custom,
    on_dashboard_instance_changed,
    on_dashboard_refused,
    on_goal_recorded,
    on_guide_finished,
    on_guide_offered,
    on_guide_started,
    on_ledger_recorded,
    on_object_observed,
    on_panel_published,
    on_radar_recorded,
    on_waiting_changed,
    on_work_recorded,
    panel_entry_fits,
    radar_entry_fits,
    work_entry_fits,
)
from kiro_crew.crew_log.emit_text import (  # noqa: F401
    _CHUNK_TEXT_CHARS,
    _ENVELOPE_HEADROOM,
    _MAX_ID_TEXT,
    _MAX_PLAN_ITEMS,
    _MAX_SHORT_TEXT,
    _WORK_MAX_NESTING,
    _WORK_NESTED_TEXT_FIELDS,
    _WORK_PLAIN_TEXT_FIELDS,
    BODY_MODE,
    BODY_MODE_REF,
    BODY_MODE_TEXT,
    WorkFieldCollisionError,
    WorkFieldError,
    WorkFieldTooDeepError,
    _append_body_entry,
    _bounded_attachment_data,
    _clip,
    _entry_line_fits,
    _fits_one_line,
    _payload_digest,
    _safe_text,
    _safe_work_value,
    _text_slices,
    safe_work_fields,
)
from kiro_crew.crew_log.emit_tree import (  # noqa: F401
    COMMIT_WAIT_SECONDS,
    _candidate_is_same_slot,
    _durable_previous,
    _parent_citation,
    _record_session_tree_decision,
    _record_session_tree_edge,
    _record_session_tree_spawn,
    _tree_settle_hooks,
    _TreeSettle,
    awaiting_commit,
    slot_previous_store,
)
from kiro_crew.crew_log.emit_turn import (  # noqa: F401
    _EST_CHARS_PER_TOKEN,
    _OTHER_SOURCE,
    _UNCLASSIFIED_LABELS,
    ACTORS,
    _turn_closer,
    close_open_tool_calls,
    on_approval_decided,
    on_approval_requested,
    on_background_completed,
    on_compaction_applied,
    on_context_composed,
    on_context_measured,
    on_message_queued,
    on_message_received,
    on_message_sent,
    on_model_selected,
    on_plan_updated,
    on_request_configured,
    on_step_completed,
    on_step_started,
    on_tool_called,
    on_tool_completed,
    on_turn_completed,
    on_turn_failed,
    on_turn_refused,
    on_turn_started,
)

# This module is the import path and patch surface of the session crew-log emitter, and every
# name is reached through it as ``kiro_crew.crew_log.emit.<name>``. It binds each name its
# owners define, so its namespace holds the definitions the unsplit module held, and it serves
# the rest from the owner holding them: the names the owners import, and the nine globals an
# owner rebinds with ``global`` -- ``emit_runtime``'s collaborator slots and counters, and
# ``emit_state``'s ``_subsystem``. A write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches
# every owner holding the name (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its responsibility:
#
#   ``emit_state`` -- the switch, the boot-path import gates, the shared maps and their lock
#   ``emit_tree`` -- a slot's lineage, the session-tree folds, the settled signal
#   ``emit_runtime`` -- the installed writer, tracker and warnings; the submit, settle and
#       supersede-repair path; flush, the shutdown drain and the loss counters; the
#       child-origin pins; session close
#   ``emit_text`` -- redaction, clipping, and fitting an entry or a body into a line
#   ``emit_session`` -- session opened, adopted, released, spawned, and its class
#   ``emit_turn`` -- a turn's start and end, messages, request, context, steps, tools,
#       approvals, plan, model, compaction, and background model calls
#   ``emit_children`` -- subagent entries on the dispatching session's log
#   ``emit_surfaces`` -- ledger, object, radar, work, panel, dashboard, card and guide entries
#   ``emit_crew`` -- the crew kind's dispatch and report entries


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.crew_log.emit_state",
    "kiro_crew.crew_log.emit_tree",
    "kiro_crew.crew_log.emit_runtime",
    "kiro_crew.crew_log.emit_text",
    "kiro_crew.crew_log.emit_session",
    "kiro_crew.crew_log.emit_turn",
    "kiro_crew.crew_log.emit_children",
    "kiro_crew.crew_log.emit_surfaces",
    "kiro_crew.crew_log.emit_crew",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: BASE's own list, unchanged.
__all__ = [
    "ACTORS",
    "CREW_LOG_ENV",
    "buffered_writes",
    "drain_for_shutdown",
    "dropped_writes",
    "enabled",
    "flush",
    "peak_buffered_writes",
    "on_approval_decided",
    "on_approval_requested",
    "on_compaction_applied",
    "on_context_composed",
    "on_context_measured",
    "on_message_queued",
    "on_message_received",
    "on_message_sent",
    "on_model_selected",
    "on_request_configured",
    "on_session_adopted",
    "on_session_closed",
    "on_session_opened",
    "on_session_released",
    "on_step_completed",
    "on_step_started",
    "on_tool_called",
    "on_tool_completed",
    "on_turn_completed",
    "on_turn_failed",
    "on_turn_refused",
    "on_turn_started",
    "reset_caches",
    "slot_previous_store",
]

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.crew_log.emit_runtime import (  # noqa: F401
        _child_liveness,
        _lost_child_origins,
        _lost_origin_reported,
        _next_limits,
        _shutdown_hook_registered,
        _tracker_instance,
        _warnings_instance,
        _writer_instance,
    )
    from kiro_crew.crew_log.emit_state import (  # noqa: F401
        _subsystem,
    )

if TYPE_CHECKING:  # names it serves from outside the emitter family, from their homes
    from kiro_crew.constants import (  # noqa: F401
        crew_log_enabled,
    )
    from kiro_crew.executors import (  # noqa: F401
        crew_log_executor,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
