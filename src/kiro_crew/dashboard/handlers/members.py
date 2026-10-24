"""Crew Members HTTP handlers — roster and per-member DM thread binding.

The Crew Members page talks to each crew member in one durable, pinned DM
thread. The thread's slot key is DERIVED (``member-<slug>``) and its binding
lives in the member's own space (``members/<slug>/dm.json``), so the mapping
survives restarts independently of the slot layer's own persistence.

Member slots are born ONLY here, with ``mode="member"``: the generic slot
create endpoint's ``_CREATABLE_MODES`` deliberately excludes it, and the
frontend's chat-ownership predicate (``isChatPageSurface``) does not admit it,
which is what keeps member threads out of the ordinary Sessions list with no
filtering code anywhere.

Dashboard-only surface: app tokens are denied outright (deny-by-default, same
posture as slot access — an app has no business enumerating the user's crews
or opening threads that speak as them).
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the Crew Members routes: every name is reached through it
# as ``kiro_crew.dashboard.handlers.members.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#   ``members_access`` -- the app-caller denial, the owner-read audit and the slug's names
#   ``members_roster`` -- the roster list and the member-log attribution it is served under
#   ``members_thread`` -- opening, binding and pinning a crewmate's DM thread
#   ``members_thread_turns`` -- the first welcome and fresh start on that thread
#   ``members_drawer`` -- one member's projections, activity, notes and recap
#   ``members_rules`` -- reading and writing a crewmate's permanent rules
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.members_access",
    "kiro_crew.dashboard.handlers.members_roster",
    "kiro_crew.dashboard.handlers.members_thread",
    "kiro_crew.dashboard.handlers.members_thread_turns",
    "kiro_crew.dashboard.handlers.members_drawer",
    "kiro_crew.dashboard.handlers.members_rules",
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
    from kiro_crew.dashboard.handlers.members_access import (  # noqa: F401
        _audit_owner_read,
        _deny_app_caller,
        _member_name_is_addressable,
        _member_names_for_slug,
        _sel,
        _slug_is_claimed_by_any_member,
    )
    from kiro_crew.dashboard.handlers.members_drawer import (  # noqa: F401
        _ACTIVITY_LIMIT,
        _ACTIVITY_PAGE,
        _parse_activity_ts,
        api_member_activity,
        api_member_briefing,
        api_member_projections,
        api_member_recap,
    )
    from kiro_crew.dashboard.handlers.members_roster import (  # noqa: F401
        _SEQ_NO_LOG,
        _SEQ_UNATTRIBUTABLE,
        _SOURCE_BUILTIN,
        _SOURCE_KIROCREW,
        _SOURCE_PACKAGE,
        _absence_is_confirmed,
        _as_epoch,
        _live_slot_has_rows,
        _logged_slugs,
        _member_is_created,
        _recency_for_row,
        _roster_only,
        _slot_flush_generation,
        _slot_has_unflushed_rows,
        _stamp_welcome_claimed,
        _transcript_holds_message,
        api_members,
        normalize_member_source,
        read_dm_thread_message_state,
        unchatted_legacy_bindings,
    )
    from kiro_crew.dashboard.handlers.members_rules import (  # noqa: F401
        api_member_rules_get,
        api_member_rules_put,
        logger,
    )
    from kiro_crew.dashboard.handlers.members_thread import (  # noqa: F401
        _bind_member_workspace,
        _guard_and_pin_private_store,
        _member_thread_slot,
        _member_workspace_project,
        _seed_member_approval,
        api_member_thread,
    )
    from kiro_crew.dashboard.handlers.members_thread_turns import (  # noqa: F401
        _FRESH_START_POLL_SECS,
        _FRESH_START_STOP_WAIT_SECS,
        _queue_pending,
        _turn_busy,
        _turn_ends_within,
        _work_pending,
        api_member_fresh_start,
        api_member_greet,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import kiro_crew.dashboard.handlers as _h  # noqa: F401
    from kiro_crew import members as members_mod  # noqa: F401
    from kiro_crew.config.loader import (  # noqa: F401
        KiroCrewConfig,
        default_project_dir,
        load_config_with_content_stamp,
    )
    from kiro_crew.config.sections import (  # noqa: F401
        MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    )
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        pin_private_agent_store,
        rehydrate_slot_from_history_async,
    )
    from kiro_crew.dashboard.chat_utils import (  # noqa: F401
        effective_session_key,
    )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        require_owner_dashboard_request,
    )
    from kiro_crew.dashboard.state import (  # noqa: F401
        DashboardState,
        request_slot_origin,
    )
    from kiro_crew.external_text import (  # noqa: F401
        redact_external_text,
    )
    from kiro_crew.members import (  # noqa: F401
        MemberSlugError,
    )
