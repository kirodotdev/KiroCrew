"""Every handler routed through the shared body guard answers 400, never 5xx.

``[]``, ``"s"``, ``5``, ``true`` and ``null`` are all VALID JSON, so
``await request.json()`` returns them happily -- and the ``.get()`` that every
one of these handlers performs next then raises ``AttributeError`` from OUTSIDE
the ``try`` that wrapped the parse. The result was a 500 for what is really
malformed client input (issue #5587).

This is enumerate-the-invariant coverage rather than one test per handler: the
table below is the list of handlers converted to
``_shared.read_bounded_json``, and it is the ratchet for the remaining sweep --
a handler converted in a later tranche gets a row here, and a row that stops
answering ``body_not_object`` fails by construction.

A later tranche must record its cap decision in ``_CAP_REGISTER`` below, which is
checked against the code: a converted call site that is not registered fails, and
a declared cap that disagrees with what the handler passes fails. Prose could not
stop ``max_bytes=None`` being copied along with a row; the register can.

Handlers are called directly with a minimal request stand-in. Going through the
router would drag in the owner-identity middleware and a real config load for
every row, and neither is the subject: the guard runs on the parsed body,
after those gates.
"""

from __future__ import annotations

import ast
import functools
import json
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import _shared as shared
from kiro_crew.dashboard.handlers import agents as agents_mod

# Marked per-test rather than module-wide: the cap-register tests below are
# synchronous, and a module-wide asyncio mark warns on every one of them.

#: Valid JSON documents that are not objects. ``None`` is the JSON literal
#: ``null``, which is distinct from an absent body.
_NON_OBJECTS = ([], "a string", 5, 1.5, True, None)


class _Req:
    """The parts of ``web.Request`` the guard and these handlers read."""

    def __init__(self, payload, method: str = "POST"):
        self._payload = payload
        self.method = method
        self.headers: dict[str, str] = {}
        self.match_info: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.can_read_body = True
        self.charset = None
        self.app = {"state": None}

    async def json(self):
        return self._payload


#: ``(handler, method)`` for each agents.py handler in this tranche. Every one
#: sits behind ``_require_owner``, neutralised by the fixture below.
_AGENTS_HANDLERS = [
    (agents_mod.api_agent_config, "PUT"),
    (agents_mod.api_default_agent, "PUT"),
    (agents_mod.api_capability_mcp_install, "POST"),
    (agents_mod.api_capability_mcp_uninstall, "POST"),
    (agents_mod.api_capability_skills_install, "POST"),
    (agents_mod.api_capability_skills_uninstall, "POST"),
]


@pytest.fixture
def _owner_allowed(monkeypatch):
    """Let the owner gate through so the body guard is what runs.

    The gate is a separate invariant with its own coverage in
    ``test_agents_endpoints_owner_auth.py``; leaving it armed here would answer
    403 to every row and the guard would never be reached.
    """

    async def _allow(_request, _operation):
        return None

    monkeypatch.setattr(agents_mod, "_require_owner", _allow)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,method", _AGENTS_HANDLERS, ids=lambda v: getattr(v, "__name__", v)
)
@pytest.mark.parametrize("payload", _NON_OBJECTS, ids=repr)
async def test_non_object_body_is_400_not_500(handler, method, payload, _owner_allowed):
    resp = await handler(_Req(payload, method=method))
    assert resp.status == 400, f"{handler.__name__} on {payload!r}: expected 400"
    assert json.loads(resp.text)["code"] == "body_not_object"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,method", _AGENTS_HANDLERS, ids=lambda v: getattr(v, "__name__", v)
)
async def test_unparseable_body_is_400_with_invalid_json(handler, method, _owner_allowed):
    """The other half of the contract: bad bytes stay ``invalid_json``.

    Without this, a guard that answered ``body_not_object`` for everything
    would satisfy the test above while losing the distinction clients switch on.
    """

    class _BadReq(_Req):
        async def json(self):
            raise ValueError("not json")

    resp = await handler(_BadReq(None, method=method))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_json"


# ── The cap register ──────────────────────────────────────────────────────────
#
# ``read_bounded_json``'s byte cap is its original safety property: it bounds a
# body BEFORE decoding on a network-reachable surface. ``max_bytes=None`` opts
# out of it, and the docstring says that choice "is a real choice, not a default
# to inherit" -- but prose cannot stop the sweep copying ``None`` along with a
# row. So every call site's decision is RECORDED here and checked against the
# code: a conversion that is not registered fails, and a registered cap that
# disagrees with what the handler actually passes fails. An unexamined
# ``max_bytes=None`` therefore cannot land silently.

_UNBOUNDED_USER_CONTENT = (
    "body carries user content with no defensible maximum size (a file's "
    "contents, an export, a fetched document, a bundle)"
)
_CONTROL_FIELDS_CAP_PENDING = (
    "body is a fixed set of control fields and SHOULD take the default cap; "
    "left uncapped only to keep this tranche behaviour-preserving, because "
    "capping moves the site off request.json() onto the streaming read and "
    "every unit test mocking json for it must be refed. Cap it in the sweep."
)
_BOUNDED_BY_DEFAULT = "small fixed-shape payload on a strict-internal route"
_BOUNDED_EXPLICIT = "explicit per-route cap, larger than the shared default"
_BOUNDED_CONTROL_FIELDS = (
    "body is a fixed set of control fields (an identifier, a flag, a number, a "
    "short name), so the shared 64 KB ceiling is right and is applied"
)

_CAP_REASONS = {
    _UNBOUNDED_USER_CONTENT,
    _CONTROL_FIELDS_CAP_PENDING,
    _BOUNDED_BY_DEFAULT,
    _BOUNDED_EXPLICIT,
    _BOUNDED_CONTROL_FIELDS,
}

#: ``module::handler`` -> (declared cap, why). Declared cap is ``"None"`` for an
#: opt-out, ``"<default>"`` for the shared 64 KB ceiling, or the literal
#: expression passed for a per-route cap.
_CAP_REGISTER: dict[str, tuple[str, str]] = {
    # Pre-existing capped sites -- the bounded read's live consumers.
    "chat_pins.py::api_chat_pins_create": ("<default>", _BOUNDED_BY_DEFAULT),
    "handlers/feedback.py::api_feedback_submit": ("<default>", _BOUNDED_BY_DEFAULT),
    "handlers/messaging.py::api_notification_agent_push": (
        "<default>",
        _BOUNDED_BY_DEFAULT,
    ),
    "handlers/notifications_push.py::api_push_notification": (
        "<default>",
        _BOUNDED_BY_DEFAULT,
    ),
    "handlers/messaging.py::api_teams_activity": (
        "TEAMS_MAX_ACTIVITY_BYTES",
        _BOUNDED_EXPLICIT,
    ),
    # agents.py tranche.
    "handlers/agents.py::api_agent_config": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/agents.py::api_default_agent": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/agents.py::api_capability_mcp_install": (
        "None",
        _CONTROL_FIELDS_CAP_PENDING,
    ),
    "handlers/agents.py::api_capability_mcp_uninstall": (
        "None",
        _CONTROL_FIELDS_CAP_PENDING,
    ),
    "handlers/agents.py::api_capability_skills_install": (
        "None",
        _CONTROL_FIELDS_CAP_PENDING,
    ),
    "handlers/agents.py::api_capability_skills_uninstall": (
        "None",
        _CONTROL_FIELDS_CAP_PENDING,
    ),
    # memory.py tranche.
    "handlers/memory.py::api_memory_preferences": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/memory.py::api_memory_projects": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/memory.py::api_memory_history": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/memory.py::api_memory_import": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/memory.py::api_memory_semantic_write": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/memory.py::api_memory_settings": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/memory.py::api_memory_consolidate": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/memory.py::api_memory_promote": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/memory.py::api_memory_embedding_model": (
        "None",
        _CONTROL_FIELDS_CAP_PENDING,
    ),
    # knowledge.py -- the 9 sites that moved off the deleted duplicate helper.
    "handlers/knowledge.py::update_item": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/knowledge.py::add_source": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/knowledge.py::rename_source": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/knowledge.py::retry_file": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/knowledge.py::skip_file": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/knowledge.py::ingest_text": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/knowledge.py::import_bundle": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/knowledge.py::batch_embed_items": ("None", _CONTROL_FIELDS_CAP_PENDING),
    "handlers/knowledge.py::add_agent_document_route": (
        "None",
        _UNBOUNDED_USER_CONTENT,
    ),
    # ---- tranche 2 ----
    # taskrunner.py: the control-field endpoints TAKE the cap rather than
    # deferring it, which is what tranche 1 could not do without rewriting its
    # handlers' mocked-json test harness. Its own harness is updated in this PR.
    "handlers/taskrunner.py::api_taskrunner_cancel": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/taskrunner.py::api_taskrunner_rename": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/taskrunner.py::api_taskrunner_retry": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/taskrunner.py::api_taskrunner_execute_plan": (
        "<default>",
        _BOUNDED_CONTROL_FIELDS,
    ),
    # taskrunner.py: these carry a spec, a plan, or a free-text prompt.
    # ``start`` looks like a path field, but its documented contract is
    # ``{"spec": "path"}`` OR ``{"spec": "__inline__:<whole spec>"}`` -- the
    # inline form puts an entire spec in the body, so capping it would 413 a
    # large spec that works today.
    "handlers/taskrunner.py::api_taskrunner_start": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_update_task": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_plan": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_update_plan": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_from_chat": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_refine": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/taskrunner.py::api_taskrunner_refine_answer": (
        "None",
        _UNBOUNDED_USER_CONTENT,
    ),
    # chat_tags.py: every payload is a tag/column identifier, a short name
    # (already truncated at _NAME_MAX), or an id array -- all capped.
    "chat_tags.py::api_chat_tag_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_tag_update": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_slot_tags": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_tag_column_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_tag_column_update": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_tag_columns_reorder": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_tags.py::api_chat_slot_drop": ("<default>", _BOUNDED_CONTROL_FIELDS),
    # ---- tranche 3 ----
    # chat_handlers.py: control-field slot mutations take the cap; the sites
    # that carry a chat message, queued-edit text, follow-up prompts, or
    # injected context/note content stay uncapped.
    "chat_handlers.py::api_chat": ("None", _UNBOUNDED_USER_CONTENT),
    "chat_handlers.py::api_chat_slot_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_end_wait": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_interrupt": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_queue_edit": ("None", _UNBOUNDED_USER_CONTENT),
    "chat_handlers.py::api_chat_slot_queue_reorder": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_reset_conversation": (
        "<default>",
        _BOUNDED_CONTROL_FIELDS,
    ),
    "chat_handlers.py::api_chat_slots_cleanup": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_agent": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_model": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slots_model": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_reasoning_effort": (
        "<default>",
        _BOUNDED_CONTROL_FIELDS,
    ),
    "chat_handlers.py::api_chat_slot_workspace": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_project": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_followup": ("None", _UNBOUNDED_USER_CONTENT),
    "chat_handlers.py::api_chat_slot_resume": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_mode": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_approve": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_color": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "chat_handlers.py::api_chat_slot_context": ("None", _UNBOUNDED_USER_CONTENT),
    "chat_handlers.py::api_chat_slot_note": ("None", _UNBOUNDED_USER_CONTENT),
    # handlers/mcp.py: identifier/flag mutations take the cap; a full server
    # config (PUT detail) and per-server toolOverrides maps (apply) do not.
    "handlers/mcp.py::api_mcp_quarantine_clear": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_toggle": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_toggle_tool": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_toggle_all": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_remove": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_server_detail": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/mcp.py::_do_mcp_apply": ("_MCP_APPLY_MAX_BODY_BYTES", _BOUNDED_EXPLICIT),
    "handlers/mcp.py::api_mcp_gateway_enable": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/mcp.py::api_mcp_gateway_set_stub": ("<default>", _BOUNDED_CONTROL_FIELDS),
    # handlers/cron.py: create/update carry the job's full agent message,
    # whose MAX_CRON_MESSAGE character bound can exceed the shared 64 KB
    # default in multibyte UTF-8 -- so they take a per-route ceiling sized to
    # the field bound; everything else is ids and flags.
    "handlers/cron.py::api_crons_create": ("_MAX_CRON_BODY_BYTES", _BOUNDED_EXPLICIT),
    "handlers/cron.py::api_cron_batch_delete": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_cron_update": ("_MAX_CRON_BODY_BYTES", _BOUNDED_EXPLICIT),
    "handlers/cron.py::api_cron_enable": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_cron_ack": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_lessons_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_lessons_delete": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_cron_folders_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/cron.py::api_cron_folders_update": ("<default>", _BOUNDED_CONTROL_FIELDS),
    # handlers/workflows.py: workflow bodies carry a script source or a
    # free-form intent/input, so only promote (fixed metadata) takes the cap.
    "handlers/workflows.py::api_workflow_definitions_create": (
        "None",
        _UNBOUNDED_USER_CONTENT,
    ),
    "handlers/workflows.py::api_workflow_definition_update": (
        "None",
        _UNBOUNDED_USER_CONTENT,
    ),
    "handlers/workflows.py::api_workflow_definition_run": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/workflows.py::api_workflow_author": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/workflows.py::api_workflow_run": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/workflows.py::api_workflow_run_intent": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/workflows.py::api_workflow_run_promote": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/workflows.py::api_workflow_run_rerun": ("None", _UNBOUNDED_USER_CONTENT),
    # handlers/files.py: bodies name paths and routing fields -- the file
    # bytes travel in api_file_write's body, which is the one uncapped site.
    "handlers/files.py::api_reveal_path": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_outbox_notify": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_slack_upload_file": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_channel_upload_file": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_workspaces_create": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_workspaces_update": ("<default>", _BOUNDED_CONTROL_FIELDS),
    "handlers/files.py::api_file_write": ("None", _UNBOUNDED_USER_CONTENT),
    "handlers/files.py::api_dashboard_config": ("<default>", _BOUNDED_CONTROL_FIELDS),
}

_DASHBOARD_DIR = Path(shared.__file__).resolve().parent.parent

#: How many rows currently ship uncapped while their body is a fixed set of
#: control fields -- i.e. endpoints whose register entry says they SHOULD take
#: the default cap, deferred only to keep this tranche behaviour-preserving.
#:
#: This number is a RATCHET, in the same shape as the repo's error-code and
#: coverage baselines: it may SHRINK as a later tranche applies those caps, and
#: it may never grow. Recording the decision (above) is what stops an unexamined
#: ``max_bytes=None`` from landing; this is what stops the recorded debt from
#: quietly becoming permanent, because otherwise the sweep could finish with
#: every one of these endpoints still unbounded and nothing would fail. The
#: 64 KB bound is the helper's original safety property (issue #490), so
#: "recorded" is not the same as "handled".
_CAP_PENDING_CEILING = 13


@functools.lru_cache(maxsize=1)
def _call_sites() -> dict[str, str]:
    """``module::handler`` -> the ``max_bytes`` expression actually passed."""
    found: dict[str, str] = {}
    for path in sorted(_DASHBOARD_DIR.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "read_bounded_json(" not in src:
            continue
        tree = ast.parse(src)
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "read_bounded_json"
            ):
                continue
            owner = None
            for fn in funcs:
                if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                    if owner is None or fn.lineno > owner.lineno:
                        owner = fn
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            rel = path.relative_to(_DASHBOARD_DIR).as_posix()
            found[f"{rel}::{getattr(owner, 'name', '?')}"] = kw.get("max_bytes", "<default>")
    return found


class TestCapRegister:
    def test_the_register_is_not_vacuous(self) -> None:
        # A refactor that moved or renamed the helper would otherwise empty the
        # scan and make every assertion below pass by finding nothing.
        assert len(_call_sites()) >= 25

    def test_every_call_site_records_a_cap_decision(self) -> None:
        unregistered = sorted(set(_call_sites()) - set(_CAP_REGISTER))
        assert not unregistered, (
            "these read_bounded_json call sites record no cap decision -- add a "
            "row with a reason rather than inheriting max_bytes=None: "
            f"{unregistered}"
        )

    def test_no_register_row_outlives_its_call_site(self) -> None:
        stale = sorted(set(_CAP_REGISTER) - set(_call_sites()))
        assert not stale, f"register rows with no matching call site: {stale}"

    def test_declared_cap_matches_the_code(self) -> None:
        mismatches = {
            key: (declared, actual)
            for key, (declared, _why) in _CAP_REGISTER.items()
            if (actual := _call_sites().get(key)) is not None and actual != declared
        }
        assert not mismatches, f"declared cap disagrees with the code: {mismatches}"

    def test_every_reason_is_one_of_the_documented_ones(self) -> None:
        bad = {k: why for k, (_c, why) in _CAP_REGISTER.items() if why not in _CAP_REASONS}
        assert not bad, f"rows citing an undocumented reason: {sorted(bad)}"

    def test_the_bounded_read_still_has_live_consumers(self) -> None:
        # The point of the register: if the sweep converts everything to None,
        # the cap becomes an opt-in nobody exercises. This fails when the last
        # bounded consumer disappears.
        bounded = [k for k, v in _call_sites().items() if v != "None"]
        assert len(bounded) >= 5, f"only {len(bounded)} bounded call site(s) left"

    def test_the_pending_cap_debt_only_shrinks(self) -> None:
        """The deferred caps are a ratchet, not a permanent state.

        Recording a cap decision stops an unexamined ``max_bytes=None`` from
        landing, but on its own it would let the sweep finish with every
        control-field endpoint still unbounded -- the decision would be written
        down and never acted on. So the count is pinned: a later tranche that
        applies a cap must lower ``_CAP_PENDING_CEILING`` with it, and one that
        adds a new deferral fails here instead of growing the debt silently.
        """
        pending = sorted(
            key for key, (_cap, why) in _CAP_REGISTER.items() if why == _CONTROL_FIELDS_CAP_PENDING
        )
        assert len(pending) <= _CAP_PENDING_CEILING, (
            f"{len(pending)} endpoints now defer their cap, over the ceiling of "
            f"{_CAP_PENDING_CEILING}. Apply the cap instead of deferring another "
            f"one: {pending}"
        )
        assert len(pending) == _CAP_PENDING_CEILING, (
            f"only {len(pending)} endpoints still defer their cap, below the "
            f"ceiling of {_CAP_PENDING_CEILING} -- lower _CAP_PENDING_CEILING to "
            f"{len(pending)} so the ratchet keeps tightening"
        )

    def test_every_deferred_cap_is_actually_uncapped(self) -> None:
        # A row claiming a DEFERRED cap while the code already caps it would
        # inflate the ceiling and make the ratchet unfalsifiable.
        sites = _call_sites()
        wrong = {
            key: sites.get(key)
            for key, (_cap, why) in _CAP_REGISTER.items()
            if why == _CONTROL_FIELDS_CAP_PENDING and sites.get(key) != "None"
        }
        assert not wrong, f"rows deferring a cap that the code already applies: {wrong}"
