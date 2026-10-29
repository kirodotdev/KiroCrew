"""HTTP routes for the conductor work ledger — the shared record between a
conductor session and the worker sessions it dispatched.

Thin mapping over :mod:`kiro_crew.work_ledger`, and the security contract is the
one :mod:`kiro_crew.dashboard.handlers.session_ledger` states: which ledger a
request touches is derived from the CALLING SESSION's identity
(``X-Session-Key``, vetted by ``_recognize_session``), never from the request
body. That is what makes the four tools' identity unforgeable — a worker has no
parameter naming its item, its conductor, or itself, so an out-of-bounds write is
unrepresentable rather than validated away.

Which half of the surface answers a call depends on what the caller RESOLVES to,
not on which spec mounted the tool:

===============================  =========================  ==========================
resolved caller                  ``brief`` / ``report``     ``read`` / ``record``
===============================  =========================  ==========================
a binding file, no ledger dir    available                  ``no_ledger`` (404)
a ledger dir, no binding file    ``not_bound`` (403)        available
both — a second-level conductor  available                  available
neither                          ``not_bound`` (403)        ``no_ledger`` (404)
===============================  =========================  ==========================

All four routes are MCP-only (no browser caller) and listed under
``server._STRICT_INTERNAL_API_PATHS`` by their shared ``/api/work-ledger``
prefix — without that entry the internal-secret call falls through to cookie auth
and every tool call fails with 403 before this module's own recognition can run.

Restricted (incognito / temporary / guest) sessions are refused: a ledger is
durable on-disk state, which is exactly what those modes promise not to leave
behind.

One spelling note that is load-bearing. Every key — the caller's own, and the
``worker_session_key`` a conductor supplies at ``bind`` — is folded through
:func:`session_ledger.ledger_key` before it reaches the store. One dashboard
session is legitimately spelled both ``dashboard_chat-X`` and ``chat-X``, so
without the fold a conductor could bind the spelling ``session_create`` returned
while the worker resolves the other one, and the worker would read ``not_bound``
against a binding that exists.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the work-ledger routes: every name is reached through it
# as ``kiro_crew.dashboard.handlers.work_ledger.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#   ``work_ledger_refusals`` -- the coded refusals, the audit row and the request plumbing
#   ``work_ledger_caller`` -- the entry gate: who the caller is and which ledger it reaches
#   ``work_ledger_recording`` -- the fit probe, entry, board lock and undo of a recorded write
#   ``work_ledger_worker`` -- the worker's brief and report
#   ``work_ledger_read`` -- the conductor's read of its board and the flags a row derives
#   ``work_ledger_conductor`` -- the conductor's record and rebuild writes
#   ``work_ledger_handback`` -- the hand-back a delivered ``session_send`` stamps
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.work_ledger_refusals",
    "kiro_crew.dashboard.handlers.work_ledger_caller",
    "kiro_crew.dashboard.handlers.work_ledger_recording",
    "kiro_crew.dashboard.handlers.work_ledger_worker",
    "kiro_crew.dashboard.handlers.work_ledger_read",
    "kiro_crew.dashboard.handlers.work_ledger_conductor",
    "kiro_crew.dashboard.handlers.work_ledger_handback",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.work_ledger_caller import (  # noqa: F401
        _caller_admission,
        _caller_key,
        _contained_channel_caller,
        _own_ledger,
        _reaches_a_channel,
        _worker_binding,
        caller_admission,
        contained_channel_caller,
        reaches_a_channel,
        session_is_bound_worker,
    )
    from kiro_crew.dashboard.handlers.work_ledger_conductor import (  # noqa: F401
        DISPATCH_FIELD,
        RECORD_ACTIONS,
        _bootstrap,
        _close_warning,
        _count_crew_mode_run,
        _count_recorded,
        _ensure,
        _forward_remote_decide,
        _record_conductor_write,
        _refuse_dispatch_without_ledger,
        _refuse_dispatched_worker,
        _refuse_unowned_worker,
        _worker_slot,
        _write,
        api_work_ledger_rebuild,
        api_work_ledger_record,
        holds_ledger,
    )
    from kiro_crew.dashboard.handlers.work_ledger_handback import (  # noqa: F401
        _HANDBACK_OPERATION,
        SendHandback,
        _awaits_answer,
        _handback_decision,
        _read_waiting_item,
        _report_identity,
        logger,
        read_send_handback,
        stamp_send_handback,
    )
    from kiro_crew.dashboard.handlers.work_ledger_read import (  # noqa: F401
        _COMPACT_ROW_FIELDS,
        _MAX_EVENT_TAIL,
        _QUERY_FALSE,
        _QUERY_TRUE,
        _find_slot,
        _passes_filters,
        _read_options,
        _remote_dispatch_on,
        _remote_row_fields,
        _row_with_flags,
        _slot_closed,
        _slot_looping,
        _slot_open,
        _slot_running,
        _tail_events,
        api_work_ledger_get,
        worker_holds_open_item,
    )
    from kiro_crew.dashboard.handlers.work_ledger_recording import (  # noqa: F401
        _BOARD_LOCKS,
        _WIDEST_COUNTER,
        _WIDEST_EVENT_TEXT,
        _WIDEST_HEX_ID,
        _WIDEST_ITEM_ID,
        _WIDEST_STAMP,
        _acting_unit,
        _baseline_fields,
        _baseline_overflows,
        _board_header,
        _board_lock,
        _conductor_unit,
        _crew_store,
        _current_item_and_board,
        _drain_before_cancelling,
        _entry_probe_with_baseline,
        _entry_with_baseline,
        _mark_goal_recorded,
        _mark_recorded,
        _refuse_if_dirty,
        _refuse_item_too_large,
        _refuse_unrecorded,
        _work_entry,
    )
    from kiro_crew.dashboard.handlers.work_ledger_refusals import (  # noqa: F401
        _CODE_STATUS,
        CODE_NOT_BOUND,
        ROUTE_CODES,
        _audit,
        _drop_nulls,
        _json_object,
        _refuse_400,
        _refuse_403,
        _refuse_404,
        _refuse_409,
        _refuse_503,
        _refuse_store_error,
        _sel,
        _validation_code,
    )
    from kiro_crew.dashboard.handlers.work_ledger_worker import (  # noqa: F401
        _CODE_DONE_WITHOUT_EVIDENCE,
        _WORKER_REPORT_FIELDS,
        STEER_CREW_LOG_OFF,
        STEER_ITEM_CLOSED,
        STEER_ITEM_TOO_LARGE,
        STEER_NO_ITEM,
        STEER_NO_UNIT,
        STEER_NOT_RECORDED,
        _report,
        _steer_unit,
        api_work_brief,
        api_work_report,
        record_person_steer_result,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        conductor_patrol,
        ledger_wake,
        session_ledger,
        work_ledger,
        work_preview,
    )
    from kiro_crew.constants import (  # noqa: F401
        env_file_display,
    )
    from kiro_crew.crew_log import emit as crew_log_emit  # noqa: F401
    from kiro_crew.crew_log.errors import (  # noqa: F401
        CrewLogError,
    )
    from kiro_crew.crew_log.resolve import (  # noqa: F401
        UNKNOWN,
        unit_for_session_key,
    )
    from kiro_crew.dashboard import (  # noqa: F401
        session_control,
    )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        _is_restricted_session,
    )
    from kiro_crew.dashboard.handlers.cron import (  # noqa: F401
        _recognize_session,
    )
    from kiro_crew.dashboard.handlers.office_slides import (  # noqa: F401
        _KeyedLocks,
    )
    from kiro_crew.dashboard.state import (  # noqa: F401
        DashboardState,
    )
    from kiro_crew.history import (  # noqa: F401
        is_incognito_transcript,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        is_channel_session_key,
    )
    from kiro_crew.metrics import work as work_metrics  # noqa: F401
    from kiro_crew.validation import (  # noqa: F401
        WORK_LEDGER_READ_SCHEMA,
        WORK_LEDGER_RECORD_SCHEMA,
        WORK_REPORT_REMOTE_DISPATCH_SCHEMA,
        WORK_REPORT_SCHEMA,
        ValidationError,
        validate_tool_args,
    )
    from kiro_crew.work_ledger import (  # noqa: F401
        WorkLedgerError,
    )
    from kiro_crew.work_vocab import (  # noqa: F401
        WORK_CONDUCTOR_FIELDS,
        WORK_MAX_DEPTH_MAX,
    )
