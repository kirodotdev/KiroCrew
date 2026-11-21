"""Shared, source-guarded Crew slot/effort and title patch seams.

The current 0.12+ adapter and the retained historical migration adapters use
exactly the same audited transformation.  This module deliberately contains
no Crew version policy or installer code; the adapter owns eligibility.
"""
from __future__ import annotations

import ast
from . import crew_shared_patch as shared
from . import crew_patch_common as common

PatchError = common.PatchError

_HELPERS = (
    "_kiro_byo_cold_selection_capabilities",
    "_kiro_byo_type_selection_active",
    "_kiro_byo_reconcile_slot_routing",
)
_BACKEND_IMPORT = "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO"
_SLOT_EFFORT_MODULE = "kiro_crew.dashboard.chat_api.slot_effort"


_KIRO_BYO_SLOT_RECONCILE_HELPER = r'''

async def _kiro_byo_reconcile_slot_routing(state: DashboardState, slot: _ChatSlot | None = None) -> bool:
    """Normalize routing-only model state across a harness transition.

    KiroCrew stores Auto (Jev) as ``slot.model == "auto"`` plus
    ``slot.jev_route == True``. Kiro BYO Type selections are a different
    routing namespace. If the configured backend changes while an existing
    conversation stays open, leaving the old routing flag in place makes the
    composer keep displaying Jev even though the BYO catalogue is already
    active.

    Reconcile routing sentinels, including idle ACP sessions whose Type belongs
    to a different Mode. Unlike a running turn, an idle live provider is NOT a
    reason to keep a stale Mode. The host session must be safely reset before
    exposing the new Type; never mutate only the UI/persisted slot selection.
    Concrete-model pins and in-flight/linked sessions remain untouched.
    """
    # Imported locally: Crew's composed chat_handlers facade does not inherit
    # the owner module's globals. This helper is rebound to facade globals.
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO

    targets = [slot] if slot is not None else list(state._slots.values())
    changed = False
    _byo_default: str | None = None

    async def _default_type() -> str:
        nonlocal _byo_default
        if _byo_default is not None:
            return _byo_default
        _byo_default = ""
        try:
            from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog
            from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
            from kiro_byo.protocol import type_selection_id as _kiro_byo_type_selection_id
            from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir

            _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
            with _kiro_byo_use_app_home(_catalog_path.parents[1]):
                _cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)
            if getattr(_cfg, "models", None) and _cfg.mode_pack and _cfg.mode_type:
                _byo_default = _kiro_byo_type_selection_id(_cfg.mode_pack, _cfg.mode_type)
        except Exception:
            logger.warning("Kiro BYO slot routing reconciliation could not load default Type", exc_info=True)
        return _byo_default

    async def _retire_idle_byo_session(
        _slot: _ChatSlot, _expected: object, _target: str, _prior: str, _prior_jev: bool
    ) -> bool:
        """Use Crew's *native* model reset transaction, never an ad-hoc pop.

        The old ACP session advertised Types from its creation Mode. Changing
        its slot field without ending that session leaves actual execution on
        the old Mode. Keep the same lock order and busy/subagent checks as
        Crew's own single-slot model picker. No automatic changes for linked,
        app-owned, running, or otherwise unverifiable sessions.
        """
        try:
            import contextlib as _contextlib
            from kiro_crew.dashboard import chat_handlers as _ch
            _slot_lock = getattr(_slot, "_lock", None)
            _pick_lock = getattr(_slot, "_model_pick_lock", None)
            _name = getattr(_slot, "key", None)
            if not _name or _slot_lock is None or _pick_lock is None:
                return False
            if getattr(_slot, "_app", "") or getattr(_slot, "linked_session_key", None):
                return False
            if not all(callable(getattr(_ch, item, None)) for item in (
                "_slot_switch_session_lock", "_switch_target_busy",
                "_subagents_attached_response", "_reset_slot_session_or_warn",
            )):
                return False
            async with _contextlib.AsyncExitStack() as _stack:
                await _stack.enter_async_context(_slot_lock)
                # A slot could have been closed/recreated while waiting.
                if state._slots.get(_name) is not _slot:
                    return False
                _key = effective_session_key(_slot)
                await _stack.enter_async_context(_ch._slot_switch_session_lock(_key))
                await _stack.enter_async_context(_pick_lock)
                if state._slots.get(_name) is not _slot or effective_session_key(_slot) != _key:
                    return False
                # A user could have pinned a concrete model while this helper
                # queued on the same locks. Such a pick always wins.
                if str(getattr(_slot, "model", "") or "").strip() != _prior or _slot.jev_route is not _prior_jev:
                    return False
                _provider = state.sessions.get_provider(_key)
                if _provider is not _expected:
                    return False
                if _ch._switch_target_busy(state, _slot, _key, _provider):
                    return False
                if getattr(_slot, "_deferred_model_switch_key", None) or getattr(_slot, "_active_fallback_model", None):
                    return False
                if getattr(_slot, "_refusal_fallback_primary", None):
                    return False
                if await _ch._subagents_attached_response(state, _slot, _key, "byo_mode_reconcile") is not None:
                    return False
                # Re-check immediately before the teardown's atomic busy gate.
                if (state.sessions.get_provider(_key) is not _provider or
                        _ch._switch_target_busy(state, _slot, _key, _provider)):
                    return False
                # Mirror Crew's model-picker commit/rollback transaction. New
                # turns may start without taking the model-picker lock, so
                # publish the new selection BEFORE awaiting the atomic reset.
                _previous = _slot.model
                _previous_jev = _slot.jev_route
                _previous_gen = _slot._model_pick_gen
                _slot.model = _target
                _slot.jev_route = False
                _slot._model_pick_gen += 1
                try:
                    _reset = await _ch._reset_slot_session_or_warn(
                        state, _slot, _key, switch_kind="model"
                    )
                except BaseException:
                    if _slot.model == _target and _slot._model_pick_gen == _previous_gen + 1:
                        _slot.model = _previous
                        _slot.jev_route = _previous_jev
                        _slot._model_pick_gen = _previous_gen
                    raise
                if (_reset is False or effective_session_key(_slot) != _key or
                        state._slots.get(_name) is not _slot):
                    if _slot.model == _target and _slot._model_pick_gen == _previous_gen + 1:
                        _slot.model = _previous
                        _slot.jev_route = _previous_jev
                        _slot._model_pick_gen = _previous_gen
                    return False
                # True: normal reset. None: session was popped but its teardown
                # raised (Crew's documented committed-with-warning case).
                _slot._dirty = True
                try:
                    _ch._broadcast_context_reset(state, _slot.key, None)
                except Exception:
                    logger.debug("Kiro BYO context reset broadcast unavailable", exc_info=True)
                return True
        except Exception:
            logger.warning("Kiro BYO idle Mode change could not reset ACP session safely", exc_info=True)
            return False

    for _slot in targets:
        # Do not mutate App-owned or linked/member slots from the global chat
        # catalogue read. Those surfaces own their own backend lifecycle.
        if getattr(_slot, "_app", "") or getattr(_slot, "linked_session_key", None):
            continue
        # The configured backend is the TARGET harness; a warm ACP provider
        # describes only the OLD harness. Reading the latter as the target left
        # official -> BYO and BYO -> official sessions permanently stale.
        # _configured_backend_for_slot also preserves Crew's member routing.
        try:
            _live = state.sessions.get_provider(effective_session_key(_slot))
            _backend = await _configured_backend_for_slot(_slot)
            if not isinstance(_backend, str):
                continue
            if _live is not None:
                _actual = getattr(getattr(_live, 'capabilities', None), 'backend', None)
                if not isinstance(_actual, str):
                    continue
            else:
                _actual = _backend
            _backend_changed = _live is not None and _actual != _backend
        except Exception:
            logger.debug("Kiro BYO slot backend probe failed during routing reconciliation", exc_info=True)
            continue

        _raw = str(getattr(_slot, "model", "") or "").strip()
        _raw_lower = _raw.lower()
        _jev = getattr(_slot, "jev_route", False) is True

        if _backend == ACP_BACKEND_KIRO_BYO:
            # Type identifiers are routing selections, not concrete pins.
            # Preserve non-default picks within the CURRENT Mode, but migrate
            # old-Mode picks even when the prior ACP session is still warm.
            _old_type = (
                _raw_lower.startswith("kiro-byo-type:")
                and _raw_lower.count(":") == 2
                and all(_raw_lower.split(":", 2)[1:])
            )
            if _jev or _raw_lower in {"", "auto", "auto:jev"} or _old_type:
                _target = await _default_type()
                _target_pack = _target.split(":", 2)[1] if _target.startswith("kiro-byo-type:") else ""
                _raw_pack = _raw_lower.split(":", 2)[1] if _old_type else ""
                if _old_type and (not _target_pack or (_target_pack == _raw_pack and not _backend_changed)):
                    continue
                if _target and (_raw != _target or _jev):
                    if _live is not None:
                        if not await _retire_idle_byo_session(_slot, _live, _target, _raw, _jev):
                            # Never show Tiga when the ACP session could still
                            # run Dyna. A busy session keeps its old policy.
                            continue
                    else:
                        _slot.model = _target
                        _slot.jev_route = False
                        _slot._dirty = True
                    changed = True
            continue

        # The reverse transition must not leak a BYO-private routing sentinel
        # into Kiro or another backend. KiroCrew 0.10 represents the official
        # Kiro backend/default selection with the empty value too, so every
        # non-BYO harness is returned to its native empty/default selection.
        if _raw_lower.startswith("kiro-byo-type:"):
            if _live is not None:
                if not await _retire_idle_byo_session(_slot, _live, "", _raw, _jev):
                    # A busy/unknown session retains the original ACP harness;
                    # changing only the composer model would misrepresent it.
                    continue
            else:
                _slot.model = ""
                _slot.jev_route = False
                _slot._dirty = True
            changed = True

    if changed:
        state.push_slots_update()
    return changed
'''


def _bind_owner_endpoint_locals(text: str) -> str:
    """Repair owner→facade global rebinding without modifying chat_handlers.py.

    This also upgrades older BYO-generated slot_effort files: insert imports
    once, after each endpoint docstring, so the references keep working when
    Crew rebinds the owner functions onto the facade's global namespace.
    """
    endpoint_names = (
        ("api_chat_slot_selection_capabilities", (
            "_kiro_byo_cold_selection_capabilities",
            "_kiro_byo_type_selection_active",
            "_kiro_byo_reconcile_slot_routing",
        )),
        ("api_chat_slot_reasoning_effort", (
            "_kiro_byo_type_selection_active",
        )),
    )
    for endpoint, helpers in endpoint_names:
        tree = common._parse(text)
        funcs = [node for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == endpoint]
        if len(funcs) != 1 or not funcs[0].body:
            raise PatchError(f"Crew composed effort endpoint {endpoint} moved")
        fn = funcs[0]
        # The imports must be local to the function. Importing them into
        # slot_effort's module globals is not sufficient after facade compose.
        bound: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom):
                if node.module in {
                    "kiro_crew.agent_sdk.backends",
                    "kiro_crew.dashboard.chat_api.slot_effort",
                }:
                    bound.update(alias.asname or alias.name for alias in node.names)
        required = {"ACP_BACKEND_KIRO_BYO", *helpers}
        if required <= bound:
            continue
        # Use a standalone import for the ID and a same-owner import for the
        # helpers. The self-import is resolved at call time, after import/compose.
        body = [
            "    # BYO: local bindings survive Crew facade composition.",
            "    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO",
            "    from kiro_crew.dashboard.chat_api.slot_effort import (",
            *(f"        {name}," for name in helpers),
            "    )",
        ]
        head = fn.body[0]
        if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)):
            insertion_line = head.end_lineno
        else:
            insertion_line = fn.lineno
        if insertion_line is None:
            raise PatchError("Crew endpoint import insertion span unavailable")
        text = common._insert_after_line(text, insertion_line, "\n".join(body) + "\n")
    return text


def _patch_slot_effort(text: str) -> str:
    """Apply BYO effort semantics plus harness-transition slot reconciliation."""
    # shared._patch_chat_handlers expects this marker to already be present or
    # an agent_sdk.backends import to exist.  slot_effort has neither in pristine
    # Crew 0.10, so add a sibling runtime import next to aiohttp first.
    if _BACKEND_IMPORT not in text:
        _span, node = common._import_span(text, "aiohttp")
        if node.end_lineno is None:
            raise PatchError("Crew 0.10 aiohttp import has no source span")
        text = common._insert_after_line(text, node.end_lineno, _BACKEND_IMPORT)
    text = shared._patch_chat_handlers(text)
    # Crew 0.12's chat_handlers facade rebinds every slot_effort function to
    # facade globals. Bind BYO-only names INSIDE the endpoint functions, not
    # in the owner's module globals (those disappear after compose()).
    text = _bind_owner_endpoint_locals(text)

    if "async def _kiro_byo_reconcile_slot_routing(" in text:
        # Replace only our owned helper when upgrading a 0.2.2 installation.
        # Otherwise a patched Gateway would retain the old cross-session bug.
        text = common._replace_owned_top_function(
            text, "_kiro_byo_reconcile_slot_routing", _KIRO_BYO_SLOT_RECONCILE_HELPER.lstrip("\r\n")
        )
    else:
        tree = common._parse(text)
        anchor_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_configured_backend_for_slot"
            ),
            None,
        )
        if anchor_node is None:
            raise PatchError("Crew 0.10 slot backend resolver seam moved")
        text = common._insert_before_line(
            text, anchor_node.lineno - 1, _KIRO_BYO_SLOT_RECONCILE_HELPER
        )

    marker = "    await _kiro_byo_reconcile_slot_routing(state, slot)\n"
    if marker not in text:
        tree = common._parse(text)
        fn = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "api_chat_slot_selection_capabilities"
            ),
            None,
        )
        if fn is None or fn.end_lineno is None:
            raise PatchError("Crew 0.10 selection-capabilities seam moved")
        lines = text.splitlines(keepends=True)
        block = "".join(lines[fn.lineno - 1 : fn.end_lineno])
        needle = "    if slot is None:\n        return slot_not_found()\n"
        if block.count(needle) != 1:
            raise PatchError("Crew 0.10 selection-capabilities slot guard moved")
        block = block.replace(needle, needle + marker, 1)
        text = "".join(lines[: fn.lineno - 1]) + block + "".join(lines[fn.end_lineno :])
    return text


_CHAT_TITLE_HELPER = r'''

async def _kiro_byo_title_model() -> str:
    """Use Crew's background-role model when the active harness is Kiro BYO."""
    def _read() -> str:
        cfg = KiroCrewConfig.load()
        if str(getattr(cfg.agent, "acp_backend", "") or "") == "kiro-byo":
            return str(cfg.agent.resolve_model("background") or "auto")
        return _TITLE_MODEL

    try:
        return await asyncio.to_thread(_read)
    except Exception:
        logger.debug("Kiro BYO title background-model resolution failed; using auto", exc_info=True)
        return _TITLE_MODEL
'''


def _patch_chat_title(text: str) -> str:
    """Make dashboard auto-title honor Crew's background role under Kiro BYO.

    Native Crew behavior stays untouched for every other backend.  This aligns
    dashboard titling with status-card/summary workers, which already resolve
    ``agent.role_models.background``.
    """
    common._parse(text)
    marker = "async def _kiro_byo_title_model() -> str:"
    patched_call = "model=await _kiro_byo_title_model(),"
    if marker not in text:
        title_anchor = '_TITLE_MODEL = "auto"'
        if text.count(title_anchor) != 1:
            raise PatchError("Crew 0.10 title model anchor moved")
        text = text.replace(title_anchor, title_anchor + _CHAT_TITLE_HELPER, 1)

    call_count = text.count("model=_TITLE_MODEL,")
    patched_count = text.count(patched_call)
    if call_count:
        if call_count != 2 or patched_count:
            raise PatchError("Crew 0.10 title one-liner call shape moved")
        text = text.replace("model=_TITLE_MODEL,", patched_call)
    elif patched_count != 2:
        raise PatchError("Crew 0.10 title one-liner call shape moved")
    return text


def is_owned_facade_effort_upgrade(previous: str, updated: str) -> bool:
    """Prove an in-place BYO 0.3.4 -> fixed 0.12 facade binding migration.

    Only the six BYO-owned *function-scope* imports may be added. AST of all
    Crew-owned statements and prior BYO logic must otherwise be identical.
    This check is used by the source installer before it allows a patched
    target file to be modified, including installations with old snapshots.
    """
    try:
        if previous == updated:
            return False
        old = ast.parse(previous)
        new = ast.parse(updated)
        expects = {
            "api_chat_slot_selection_capabilities": {
                "kiro_crew.agent_sdk.backends": {"ACP_BACKEND_KIRO_BYO"},
                _SLOT_EFFORT_MODULE: {
                    "_kiro_byo_cold_selection_capabilities",
                    "_kiro_byo_type_selection_active",
                    "_kiro_byo_reconcile_slot_routing",
                },
            },
            "api_chat_slot_reasoning_effort": {
                "kiro_crew.agent_sdk.backends": {"ACP_BACKEND_KIRO_BYO"},
                _SLOT_EFFORT_MODULE: {"_kiro_byo_type_selection_active"},
            },
            "_kiro_byo_cold_selection_capabilities": {
                "kiro_crew.agent_sdk.backends": {"ACP_BACKEND_KIRO_BYO"},
            },
            "_kiro_byo_reconcile_slot_routing": {
                "kiro_crew.agent_sdk.backends": {"ACP_BACKEND_KIRO_BYO"},
            },
        }
        removed: set[str] = set()
        for node in new.body:
            if not isinstance(node, ast.AsyncFunctionDef) or node.name not in expects:
                continue
            targets = expects[node.name]
            kept: list[ast.stmt] = []
            for statement in node.body:
                if (isinstance(statement, ast.ImportFrom)
                        and statement.level == 0
                        and statement.module in targets
                        and {alias.name for alias in statement.names} == targets[statement.module]
                        and all(alias.asname is None for alias in statement.names)):
                    removed.add(node.name + ':' + statement.module)
                else:
                    kept.append(statement)
            node.body = kept
        required = {name + ':' + module for name, owners in expects.items()
                    for module in owners}
        return (removed == required
                and ast.dump(old, include_attributes=False)
                == ast.dump(new, include_attributes=False))
    except (SyntaxError, ValueError, TypeError):
        return False
