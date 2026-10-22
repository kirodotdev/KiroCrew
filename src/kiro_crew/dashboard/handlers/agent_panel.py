"""HTTP routes for a crew's webview.

Two halves with deliberately different auth, because they are different acts:

* **Publishing** (``/api/agent-panel/*``) is MCP-only and strict-internal. The
  crew a call writes to is derived from the CALLING SESSION's identity
  (``X-Session-Key``, vetted by ``_recognize_session``) and then from that
  session's agent -- never from the request body. So a crew can only ever
  publish its own webview, and raw HTTP with no recognized session identity is
  refused. Restricted (incognito/temporary/guest) sessions are refused too: a
  published panel is durable on-disk state, which is exactly what those modes
  promise not to leave behind.

  Both publish routes are listed in ``server._STRICT_INTERNAL_API_PATHS`` --
  without that entry the internal-secret call falls through to cookie auth and
  every publish fails with 403.

* **Reading** (``/api/members/{slug}/panel``) is an ordinary cookie-authed
  dashboard route, because the drawer is what reads it. It is a read: nothing
  under it can publish or edit a panel.
"""

# This module is the agent panel handler's facade: the server, routes and tests import
# and patch every name through it, as ``kiro_crew.dashboard.handlers.agent_panel.<name>``.
# A read of a name an owner holds is served from that owner, and a write --
# ``monkeypatch.setattr``, ``mock.patch`` -- reaches every module holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module. It keeps the route
# table. New work goes to the owner of its responsibility:
#
#   ``agent_panel_read`` -- the panel record, its board card, and the drawer's read route
#   ``agent_panel_publish`` -- vetting a publishing caller, and the two publish routes
#   ``agent_panel_dashboard`` -- the dynamic dashboard's ``/api/agent-panel/dashboard/`` routes
#   ``agent_panel_snapshot`` -- the picture of a crewmate's own page, ``.../dashboard/snapshot``

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers.agent_panel_dashboard import (
    api_dashboard_apply,
    api_dashboard_fields,
    api_dashboard_preview,
    api_dashboard_rollback,
    api_dashboard_save,
    api_dashboard_templates,
    api_dashboard_types,
    api_dashboard_write,
)
from kiro_crew.dashboard.handlers.agent_panel_publish import (
    api_agent_panel_publish,
    api_agent_panel_templates,
)
from kiro_crew.dashboard.handlers.agent_panel_read import api_member_panel
from kiro_crew.dashboard.handlers.agent_panel_snapshot import api_dashboard_snapshot

logger = logging.getLogger(__name__)


def register_agent_panel_routes(app: web.Application) -> None:
    app.router.add_get("/api/agent-panel/templates", api_agent_panel_templates)
    app.router.add_post("/api/agent-panel/publish", api_agent_panel_publish)
    # The dynamic dashboard's agent surface. Under the SAME prefix, which is what
    # gives it the same auth: ``server._STRICT_INTERNAL_API_PATHS`` lists
    # ``/api/agent-panel`` and the middleware matches by prefix, so these are
    # MCP-only and strict-internal without a second entry. That matters here as
    # much as for publish -- a write lands in the crewmate's own crew log, so a
    # caller holding only a dashboard cookie must not reach it.
    app.router.add_get("/api/agent-panel/dashboard/fields", api_dashboard_fields)
    # The data-type catalog, a pure read and on this prefix anyway: it answers how far
    # the calling crewmate's own folds have been folded, which is state about that
    # crewmate and nobody else's to read.
    app.router.add_get("/api/agent-panel/dashboard/types", api_dashboard_types)
    app.router.add_post("/api/agent-panel/dashboard/write", api_dashboard_write)
    # The save. Under the same prefix for the same reason, and with one more of its
    # own: it PUTS A PAGE IN FORCE FOR A CREWMATE -- the package it writes is bound to
    # the caller this prefix's gate vetted. A cookie-only caller reaching it could name
    # any live session and put a page on whatever crew that resolved to.
    app.router.add_post("/api/agent-panel/dashboard/save", api_dashboard_save)
    # The page's own four, under the same prefix and so with the same auth. A preview
    # and an apply reach a crewmate's instance store and its crew log, so a caller
    # holding only a dashboard cookie must not reach them either.
    app.router.add_get("/api/agent-panel/dashboard/templates", api_dashboard_templates)
    app.router.add_post("/api/agent-panel/dashboard/preview", api_dashboard_preview)
    app.router.add_post("/api/agent-panel/dashboard/apply", api_dashboard_apply)
    app.router.add_post("/api/agent-panel/dashboard/rollback", api_dashboard_rollback)
    # A picture of the caller's own page in force, over its real data. On this prefix
    # because that picture is the crewmate's own record, which is nobody else's to ask for.
    app.router.add_get("/api/agent-panel/dashboard/snapshot", api_dashboard_snapshot)
    # The drawer's read. NOT under /api/agent-panel: that prefix is
    # strict-internal (MCP-only), and this one is called by the browser.
    app.router.add_get("/api/members/{slug}/panel", api_member_panel)


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.agent_panel_read",
    "kiro_crew.dashboard.handlers.agent_panel_publish",
    "kiro_crew.dashboard.handlers.agent_panel_dashboard",
    "kiro_crew.dashboard.handlers.agent_panel_snapshot",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.agent_panel_dashboard import (  # noqa: F401
        _SESSION_PACKAGE_REFUSAL,
        _apply_own_package,
        _await_check,
        _broadcast_changed,
        _credential_shaped_names,
        _current_values,
        _dashboard_artifact_store,
        _dashboard_block,
        _folded_through,
        _history_session,
        _instance_body,
        _instance_history,
        _instance_refusal,
        _mistake_book,
        _page_binding,
        _page_check,
        _record_refusal,
        _resolve_dashboard_caller,
        _template_matches,
        _templates_for_agent,
        _writer_key,
        field_sources,
        read_instance,
    )
    from kiro_crew.dashboard.handlers.agent_panel_publish import (  # noqa: F401
        _live_session_key,
        _resolve_publishing_crew,
    )
    from kiro_crew.dashboard.handlers.agent_panel_read import (  # noqa: F401
        _PANEL_SERVED_KEYS,
        _PANEL_WITHHELD_KEYS,
        _SHARED_KEY_LOGGED,
        _SHARED_KEY_WARN_CAP,
        BOARD_STALE_AFTER_SECONDS,
        _card_page,
        _card_slot,
        _is_work_board,
        _owned_by,
        _panel_meta,
        _panel_record,
        _panel_slot,
        _publish_derived_card,
        _published_record,
        _read_and_compose,
        _with_board_numbers,
        owned_panel_record,
    )
    from kiro_crew.dashboard.handlers.agent_panel_snapshot import (  # noqa: F401
        BUSY,
        NOT_COMPOSED,
        SHOT_FILE_MODE,
        SHOT_FILE_PREFIX,
        SHOT_FILE_SUFFIX,
        SHOT_KEEP,
        SHOT_ROOT,
        STATUS_IMAGE,
        STATUS_NO_IMAGE,
        _await_run,
        _no_image,
        _NoPage,
        _now_ms,
        _older_page,
        _page_in_force,
        _shot_root,
        _trim,
        spool_png,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import agent_panel  # noqa: F401
    from kiro_crew import dashboard_agentic  # noqa: F401
    from kiro_crew import dashboard_package  # noqa: F401
    from kiro_crew import dashboard_types  # noqa: F401
    from kiro_crew import pipeline_board_contract  # noqa: F401
    from kiro_crew import members as members_mod  # noqa: F401
    from kiro_crew.artifact_store import dashboard_package as _dashboard  # noqa: F401
    from kiro_crew.artifact_store.model import ArtifactValidationError  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.crew_log import emit as crew_log_emit  # noqa: F401
    from kiro_crew.crew_log import projection  # noqa: F401
    from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session  # noqa: F401
    from kiro_crew.dashboard.handlers.cron import _recognize_session  # noqa: F401
    from kiro_crew.dashboard.handlers.dashboard_page_checks import (  # noqa: F401
        MANUAL_FIELDS_NOTE,
        page_rendering,
        shape_warnings,
        skipped_note,
        theme_warnings,
    )
    from kiro_crew.dashboard.handlers.members import (  # noqa: F401
        _deny_app_caller,
        _member_names_for_slug,
        _member_thread_slot,
        _slug_is_claimed_by_any_member,
    )
    from kiro_crew.dashboard.handlers.session_ledger import _session_unit  # noqa: F401
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
        is_owner_dashboard_request,
    )
    from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key  # noqa: F401
    from kiro_crew.history import is_incognito_transcript  # noqa: F401
    from kiro_crew.members import MemberSlugError, is_readable_member_name  # noqa: F401
    from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F401
    from kiro_crew.sel import sel  # noqa: F401
    from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS  # noqa: F401
    from kiro_crew.validation import (  # noqa: F401
        DASHBOARD_SAVE_SCHEMA,
        DASHBOARD_WRITE_SCHEMA,
        PANEL_PUBLISH_SCHEMA,
        ValidationError,
        validate_tool_args,
    )
    from kiro_crew.work_vocab import WORK_FOLD_NAME, WorkBoardView  # noqa: F401
